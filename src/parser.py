import fitz
import hashlib
from pathlib import Path
import pytesseract
from PIL import Image
from src.config import get_config

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


def _ocr_page(page, tables) -> str:
    """
    Rasterizes the page and runs Tesseract (English/Hindi/Sanskrit), appending any
    detected tables as Markdown. Returns the OCR text, or None if OCR fails or is empty
    (so the caller can fall back to text-layer extraction).
    """
    try:
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        ocr_text = pytesseract.image_to_string(img, lang='eng+hin+san').strip()
    except Exception:
        return None

    if not ocr_text:
        return None

    table_markdowns = _tables_to_markdown(tables)
    if table_markdowns:
        ocr_text += "\n\n### [Structured Table Data]\n" + "\n\n".join(table_markdowns)
    return ocr_text


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


def extract_page_text_and_tables(page, ocr_mode: str = "auto", ocr_min_chars: int = 40) -> str:
    """
    Extracts text from a page while detecting tables, honoring the OCR policy:
      - "never":  always use the vector text layer, never OCR.
      - "always": OCR the page image (falling back to the text layer if OCR fails).
      - "auto":   OCR only when the text layer is thinner than `ocr_min_chars`
                  (i.e. a likely-scanned page), otherwise use the text layer.
    """
    try:
        tables = page.find_tables()
    except Exception:
        tables = None

    # Decide whether to OCR this page.
    if ocr_mode == "always":
        do_ocr = True
    elif ocr_mode == "auto":
        # Cheap probe of the raw text layer to detect scanned/image-only pages.
        do_ocr = len(page.get_text("text").strip()) < ocr_min_chars
    else:  # "never"
        do_ocr = False

    if do_ocr:
        ocr_text = _ocr_page(page, tables)
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

    for page_num in range(len(doc)):
        if progress_callback:
            progress_callback(page_num + 1, len(doc))
        page = doc.load_page(page_num)
        text = extract_page_text_and_tables(page, ocr_mode=ocr_mode, ocr_min_chars=ocr_min_chars)
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
