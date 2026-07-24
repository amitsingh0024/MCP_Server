import uuid

def chunk_document(pages: list, chunk_size: int = 2500, chunk_overlap: int = 250) -> list:
    """
    Chunks a list of pages into paragraph-aligned text chunks.
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

                # Yield this oversized paragraph as its own chunk
                chunks.append({
                    "chunk_id": str(uuid.uuid4()),
                    "page_start": page_num,
                    "page_end": page_num,
                    "text_content": p
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
