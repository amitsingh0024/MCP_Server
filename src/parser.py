import io
import re
import fitz
import logging
import hashlib
from pathlib import Path
from src.config import get_config

logger = logging.getLogger("pdfspecs.parser")


# ---------------------------------------------------------------------------
# Text-layer corruption detection
# ---------------------------------------------------------------------------
# Many of these PDFs embed subset fonts with a broken/absent ToUnicode CMap. Extraction
# then "succeeds" and returns plenty of characters — but the Devanagari comes back spliced
# with Latin letters, digits and raw control bytes, e.g.
#     'तB समासेना'टिवधा मूढगभ7गितdिI'टा'   (should be 'तत्र समासेनाष्टविधा मूढगर्भगतिर्दृष्टा')
#
# Measured on this corpus: 3,740 of 5,384 pages (69%) are affected. The old `auto` rule —
# OCR when the text layer is shorter than ocr_min_chars — never fires on them, because the
# corrupted text is *long*. Length says nothing about correctness, so we test quality too.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")        # C0 controls, excl. \t \n \r
_PUA = re.compile(r"[-]")                       # private-use: unmapped glyphs
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# A Devanagari codepoint directly abutting an ASCII letter/digit. Real text separates
# scripts with whitespace or punctuation; this adjacency is a decoding artefact.
_SCRIPT_MIX = re.compile(r"[ऀ-ॿ][A-Za-z0-9]|[A-Za-z0-9][ऀ-ॿ]")

# Hits required before a page is called corrupted. >1 so a single odd character doesn't
# cost a 5s OCR pass on an otherwise clean page.
CORRUPTION_MIN_HITS = 2


def corruption_score(text: str) -> int:
    """Count decoding-failure signals in an extracted text layer."""
    if not text:
        return 0
    return (len(_CTRL.findall(text))
            + len(_PUA.findall(text))
            + text.count("�")
            + len(_SCRIPT_MIX.findall(text)))


def looks_corrupted(text: str, min_hits: int = CORRUPTION_MIN_HITS) -> bool:
    """True when the text layer shows signs of a broken font encoding."""
    return corruption_score(text) >= min_hits


def _tables_to_markdown(tables) -> list:
    """Renders detected tables to Markdown, skipping any that fail to convert."""
    table_markdowns = []
    if not tables or not tables.tables:
        return table_markdowns
    for table in tables:
        try:
            md = table.to_markdown()
            if md:
                table_markdowns.append(md)
        except Exception:
            pass
    return table_markdowns


def _extract_text_layer(page, tables) -> str:
    """
    Extracts the vector text layer, discarding raw text inside detected table
    boundaries and appending those tables as clean Markdown instead (avoids
    double/scrambled text).
    """
    if not tables or not tables.tables:
        return page.get_text("text").strip()

    table_markdowns = _tables_to_markdown(tables)

    # Get all text blocks
    # block shape: (x0, y0, x1, y1, "text", block_no, block_type)
    blocks = page.get_text("blocks")

    filtered_blocks = []
    for b in blocks:
        x0, y0, x1, y1, text, block_no, block_type = b

        # Check if block overlaps significantly with any table bounding box
        is_inside_table = False
        for tab in tables:
            tx0, ty0, tx1, ty1 = tab.bbox
            # If the block is largely inside the table bbox
            # We use a slight padding overlap check
            if x0 >= (tx0 - 2) and x1 <= (tx1 + 2) and y0 >= (ty0 - 2) and y1 <= (ty1 + 2):
                is_inside_table = True
                break

        if not is_inside_table and text.strip():
            filtered_blocks.append(b)

    # Sort blocks top-to-bottom, left-to-right
    filtered_blocks.sort(key=lambda x: (x[1], x[0]))

    # Reassemble text blocks
    text_parts = [b[4].strip() for b in filtered_blocks]
    page_text = "\n\n".join(text_parts)

    # Append markdown tables
    if table_markdowns:
        page_text += "\n\n### [Structured Table Data]\n" + "\n\n".join(table_markdowns)

    return page_text.strip()


def extract_page_text_and_tables(page, tables=None, ocr_provider=None, ocr_mode: str = "auto",
                                 ocr_min_chars: int = 40) -> str:
    """
    Extracts text from a page while detecting tables, honoring the OCR policy:
      - "never":  always use the vector text layer, never OCR.
      - "always": OCR the page image (falling back to the text layer if OCR fails).
      - "auto":   OCR only when the text layer is thinner than `ocr_min_chars`
                  (i.e. a likely-scanned page), otherwise use the text layer.

    `tables` may be passed in by the caller (which usually already has it); we only detect
    them here when it isn't. Previously this argument was accepted and then immediately
    overwritten, so find_tables() ran twice per page and the caller's value was discarded.

    `ocr_provider` is a zero-arg callable returning an OCRProvider, not a provider — so a
    document whose pages all have a text layer never constructs one (loading an OCR model
    is expensive, and for Paddle it can fail outright).
    """
    if tables is None:
        try:
            tables = page.find_tables()
        except Exception:
            tables = None

    # Decide whether to OCR this page.
    if ocr_mode == "always":
        do_ocr = True
    elif ocr_mode == "auto":
        # Probe the raw text layer for BOTH failure modes:
        #   * too thin   -> scanned / image-only page, no text layer to speak of
        #   * corrupted  -> text layer present but mis-decoded (broken ToUnicode CMap)
        # Only the first was checked before, which missed 69% of this corpus: the
        # corrupted pages are long, so the length test always passed them through.
        probe = page.get_text("text").strip()
        do_ocr = len(probe) < ocr_min_chars or looks_corrupted(probe)
    else:  # "never"
        do_ocr = False

    if do_ocr and ocr_provider is not None:
        provider = ocr_provider() if callable(ocr_provider) else ocr_provider
        if provider is not None:
            ocr_text = provider.ocr_page(page, tables)
            if ocr_text is not None:
                return ocr_text
            # OCR failed or produced nothing -> fall back to text-layer extraction.

    return _extract_text_layer(page, tables)

def parse_pdf(file_path: Path, progress_callback=None) -> dict:
    """
    Parses a PDF file, calculates its SHA-256 hash, and extracts page-by-page text with tables.
    Returns:
        dict: {
            "sha256": str,
            "filename": str,
            "size_bytes": int,
            "pages": [{"page_num": int, "text": str}, ...]
        }
    """
    if not file_path.exists():
        raise FileNotFoundError(f"PDF file not found at: {file_path}")
    
    # Compute SHA-256 hash for diff-aware indexing
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    
    sha256 = sha256_hash.hexdigest()
    
    # Open and parse the document page by page
    doc = fitz.open(str(file_path))
    pages = []
    
    config = get_config()
    ocr_mode = getattr(config, "ocr_mode", "auto")
    ocr_min_chars = getattr(config, "ocr_min_chars", 40)

    # Build the OCR provider LAZILY, on the first page that actually needs it, and cache
    # it for the rest of the document. Constructing it up front loaded an OCR model for
    # every parse — including documents with a perfectly good text layer that never OCR
    # a single page — and any construction failure took the whole parse down with it.
    from src.ocr import get_ocr_provider
    _provider_cache = {}

    def ocr_provider():
        if "p" not in _provider_cache:
            try:
                _provider_cache["p"] = get_ocr_provider(getattr(config, "ocr_provider", "tesseract"))
            except Exception as e:
                logger.error("Could not initialise OCR provider (%s: %s); scanned pages in "
                             "this document will extract as empty.", type(e).__name__, e)
                _provider_cache["p"] = None
        return _provider_cache["p"]

    for page_num in range(len(doc)):
        if progress_callback:
            progress_callback(page_num + 1, len(doc))
        # Isolate per-page failures: one malformed page used to abort the whole document,
        # losing every other page with it. Record it empty and keep going.
        try:
            page = doc.load_page(page_num)

            try:
                tables = page.find_tables()
            except Exception:
                tables = None

            text = extract_page_text_and_tables(page, tables, ocr_provider=ocr_provider,
                                                ocr_mode=ocr_mode,
                                                ocr_min_chars=ocr_min_chars)
        except Exception as e:
            logger.warning("Failed to extract page %d of %s (%s: %s); recording it empty.",
                           page_num + 1, file_path.name, type(e).__name__, e)
            text = ""
        pages.append({
            "page_num": page_num + 1,
            "text": text
        })

    doc.close()
    
    return {
        "sha256": sha256,
        "filename": file_path.name,
        "size_bytes": file_path.stat().st_size,
        "pages": pages
    }
