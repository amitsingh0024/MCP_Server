import re
import uuid

# Sentence boundaries. Beyond ASCII terminators this must include the Devanagari danda
# `।` (U+0964) and double danda `॥` (U+0965), which end sentences and verse lines in
# Sanskrit and Hindi. A Devanagari passage contains no ASCII terminators at all, so an
# ASCII-only pattern finds zero boundaries and degrades to blind mid-word splitting.
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+|\n+")

# ---------------------------------------------------------------------------
# Structural (record-aware) chunking
# ---------------------------------------------------------------------------
# These texts are a structured export, not free prose. Every entry looks like:
#
#     Arshas(Hemorrhoids) — Shloka 52.0
#     Chikitsa Sthana | Chapter 14
#     Sanskrit: > हरिद्राचूर्णसंयुक्तं सुधाक्षीरं प्रलेपनम्|
#     Meaning: Turmeric powder mixed with fresh milk is used for anointing.
#     Description: Arsha (Piles)
#
# Measured over 1,974 pages: 7,999 shloka headers, 7,999 `Sanskrit:` labels and 7,991
# `Meaning:` labels — essentially 1:1. Chunking on those record boundaries instead of a
# character budget fixes two things at once:
#
#   * Page splits stop mattering. A record is a record whether or not it straddles a page
#     break, so a verse is never separated from its own translation — which happens today
#     (e.g. sushruta_part01 p130 ends on a shloka whose Meaning: begins on p131).
#   * Retrieval gets a natural unit. One self-contained shloka per chunk, citable by page.
#
# Documents without this structure fall back to the paragraph chunker below.
_RECORD_HEADER = re.compile(r"^.*?—\s*Shloka\s*[\d.]+\s*$", re.MULTILINE)
_LABEL = re.compile(
    r"^(Sanskrit|Meaning|Description|Specification|Dushya|Symptoms|"
    r"Treatment Principle|Method of Preparation)\s*:", re.IGNORECASE)
# A page whose text ends mid-sentence continues onto the next one; joining those with a
# newline leaves a false break inside the sentence.
_ENDS_SENTENCE = re.compile(r"[.!?।॥:|]\s*$")


def _join_pages(pages: list) -> tuple:
    """
    Concatenate page texts into one stream, returning (text, [(offset, page_num), ...]).

    Where a page ends mid-sentence and the next doesn't open a new record or label, the
    two are joined with a space rather than a newline, so a sentence broken by the page
    break is reassembled ("...prescribed according to the" + "deranged Dosa involved...").
    """
    parts, offsets, cursor = [], [], 0
    for idx, page in enumerate(pages):
        text = (page.get("text") or "").strip()
        offsets.append((cursor, page["page_num"]))
        if not text:
            continue
        if parts:
            prev = parts[-1]
            first_line = text.lstrip().split("\n", 1)[0]
            continues = (not _ENDS_SENTENCE.search(prev)
                         and not _LABEL.match(first_line)
                         and not _RECORD_HEADER.match(first_line))
            sep = " " if continues else "\n"
            parts.append(sep)
            cursor += len(sep)
            offsets[-1] = (cursor, page["page_num"])
        parts.append(text)
        cursor += len(text)
    return "".join(parts), offsets


def _page_at(offset: int, offsets: list) -> int:
    """Page number containing a character offset in the joined stream."""
    page = offsets[0][1] if offsets else 1
    for start, num in offsets:
        if start <= offset:
            page = num
        else:
            break
    return page


def _record_spans(text: str) -> list:
    """
    Return [(start, end), ...] for each record in the joined text.

    A record starts at its `... — Shloka N.N` header. Titles sometimes wrap onto the line
    above ("Kushta\\n(Obstinate skin diseases...) — Shloka 52.0"), so a short preceding
    line that isn't a label and doesn't close a sentence is pulled in as part of the title.
    """
    starts = []
    for m in _RECORD_HEADER.finditer(text):
        start = m.start()
        prev_break = text.rfind("\n", 0, start - 1)
        prev_line = text[prev_break + 1:start].strip() if prev_break != -1 else ""
        if (prev_line and len(prev_line) < 60
                and not _LABEL.match(prev_line)
                and not _ENDS_SENTENCE.search(prev_line)):
            start = prev_break + 1
        starts.append(start)

    if not starts:
        return []
    # Text before the first header (front matter) becomes its own span.
    bounds = ([0] if starts[0] > 0 else []) + starts
    return [(s, e) for s, e in zip(bounds, bounds[1:] + [len(text)]) if text[s:e].strip()]


def _split_oversized(text: str, chunk_size: int, chunk_overlap: int) -> list:
    """
    Split a single paragraph longer than chunk_size into pieces of at most chunk_size.

    Prefers sentence boundaries, falls back to a hard character split for a sentence that
    is itself oversized, and carries chunk_overlap between consecutive pieces. Without
    this an oversized paragraph was emitted verbatim as one unbounded chunk — the common
    case for OCR output, which often arrives as a single blob per page.
    """
    units = []
    for piece in _SENTENCE_END.split(text):
        if not piece or not piece.strip():
            continue
        if len(piece) <= chunk_size:
            units.append(piece)
        else:
            for i in range(0, len(piece), chunk_size):
                units.append(piece[i:i + chunk_size])

    out = []
    current, current_len = [], 0
    for unit in units:
        delimiter_len = 1 if current else 0
        if current and current_len + len(unit) + delimiter_len > chunk_size:
            out.append(" ".join(current))
            # Carry the tail of this piece into the next, bounded by chunk_overlap.
            overlap, overlap_len = [], 0
            for prev in reversed(current):
                prev_delim = 1 if overlap else 0
                if overlap_len + len(prev) + prev_delim <= chunk_overlap:
                    overlap.insert(0, prev)
                    overlap_len += len(prev) + prev_delim
                else:
                    break
            current, current_len = overlap, overlap_len
        current.append(unit)
        current_len += len(unit) + (1 if len(current) > 1 else 0)

    if current:
        out.append(" ".join(current))
    return out


def chunk_by_records(pages: list, chunk_size: int, chunk_overlap: int) -> list:
    """
    Chunk on record boundaries — one shloka (verse + translation + metadata) per chunk.

    Returns [] when the document has no record structure, so the caller can fall back to
    paragraph chunking.
    """
    text, offsets = _join_pages(pages)
    spans = _record_spans(text)
    if not spans:
        return []

    chunks = []
    for start, end in spans:
        body = text[start:end].strip()
        if not body:
            continue
        # A record longer than chunk_size is split, but only at sentence boundaries, and
        # every piece keeps the record's page range so citations stay correct.
        # A header with no body ("Entry — Shloka 2.0") is a real but empty record in the
        # source. Indexing it only produces a lexical hit on its title with nothing behind it.
        if len(body) < 30:
            continue
        pieces = ([body] if len(body) <= chunk_size
                  else _split_oversized(body, chunk_size, chunk_overlap))
        for piece in pieces:
            chunks.append({
                "chunk_id": str(uuid.uuid4()),
                "page_start": _page_at(start, offsets),
                "page_end": _page_at(end - 1, offsets),
                "text_content": piece,
            })
    return chunks


def chunk_document(pages: list, chunk_size: int = 2500, chunk_overlap: int = 250) -> list:
    """
    Chunk a document, preferring record-aware splitting and falling back to paragraphs.

    Args:
        pages (list): List of dicts like [{"page_num": int, "text": str}]
        chunk_size (int): Max character count per chunk.
        chunk_overlap (int): Max overlap character count.
    Returns:
        list: List of dicts: [
            {
                "chunk_id": str,
                "page_start": int,
                "page_end": int,
                "text_content": str
            }, ...
        ]
    """
    if not pages:
        return []

    # Structural chunking keeps each shloka whole across page breaks; see _record_spans.
    records = chunk_by_records(pages, chunk_size, chunk_overlap)
    if records:
        return records

    chunks = []
    current_paragraphs = []
    current_char_count = 0
    page_start = 1

    for page in pages:
        page_num = page["page_num"]
        text = page["text"]

        # Split page text into clean paragraphs
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

        for p in paragraphs:
            p_len = len(p)

            # Case 1: Single paragraph exceeds chunk_size
            if p_len > chunk_size:
                # Flush the current buffer first if there is anything in it
                if current_paragraphs:
                    chunk_text = "\n\n".join(current_paragraphs)
                    chunks.append({
                        "chunk_id": str(uuid.uuid4()),
                        "page_start": page_start,
                        "page_end": page_num,
                        "text_content": chunk_text
                    })
                    current_paragraphs = []
                    current_char_count = 0

                # Split the oversized paragraph rather than emitting it whole.
                for piece in _split_oversized(p, chunk_size, chunk_overlap):
                    chunks.append({
                        "chunk_id": str(uuid.uuid4()),
                        "page_start": page_num,
                        "page_end": page_num,
                        "text_content": piece
                    })
                page_start = page_num
                continue

            # Case 2: Adding this paragraph exceeds chunk_size limit
            # Calculate length with newlines if not the first paragraph in buffer
            delimiter_len = 2 if current_paragraphs else 0
            if current_char_count + p_len + delimiter_len > chunk_size:
                chunk_text = "\n\n".join(current_paragraphs)
                chunks.append({
                    "chunk_id": str(uuid.uuid4()),
                    "page_start": page_start,
                    "page_end": page_num,
                    "text_content": chunk_text
                })

                # Compute overlapping paragraphs from the tail of the current buffer
                overlap_paragraphs = []
                overlap_len = 0
                for op in reversed(current_paragraphs):
                    op_len = len(op)
                    op_delimiter = 2 if overlap_paragraphs else 0
                    if overlap_len + op_len + op_delimiter <= chunk_overlap:
                        overlap_paragraphs.insert(0, op)
                        overlap_len += op_len + op_delimiter
                    else:
                        break

                current_paragraphs = overlap_paragraphs
                current_char_count = overlap_len
                page_start = page_num

            # Append paragraph to buffer
            current_paragraphs.append(p)
            current_char_count += p_len + (2 if len(current_paragraphs) > 1 else 0)

    # Flush any remaining paragraphs in the buffer
    if current_paragraphs:
        chunk_text = "\n\n".join(current_paragraphs)
        chunks.append({
            "chunk_id": str(uuid.uuid4()),
            "page_start": page_start,
            "page_end": pages[-1]["page_num"],
            "text_content": chunk_text
        })

    return chunks
