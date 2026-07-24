import fitz
import hashlib
from pathlib import Path
import pytesseract
from PIL import Image
from src.config import get_config

def extract_page_text_and_tables(page, enable_ocr: bool = False) -> str:
    """
    Extracts text from a page while detecting tables.
    Discards raw text inside table boundaries and appends the tables
    formatted as Markdown to avoid double/scrambled text.
    If enable_ocr is True, parses the whole page image using Tesseract 
    (with Hindi/Sanskrit/English support) instead of extracting raw vector text.
    """
    try:
        tables = page.find_tables()
    except Exception:
        tables = None

    if enable_ocr:
        try:
            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            # Use English, Hindi, and Sanskrit language packs
            ocr_text = pytesseract.image_to_string(img, lang='eng+hin+san')
            page_text = ocr_text.strip()
            
            # If we found tables, append them as markdown as a structured fallback
            if tables and tables.tables:
                table_markdowns = []
                for table in tables:
                    try:
                        md = table.to_markdown()
                        if md:
                            table_markdowns.append(md)
                    except Exception:
                        pass
                if table_markdowns:
                    page_text += "\n\n### [Structured Table Data]\n" + "\n\n".join(table_markdowns)
            
            return page_text
        except Exception as e:
            # Fallback to standard extraction if OCR fails
            pass

    if not tables or not tables.tables:
        return page.get_text("text").strip()

    # Convert tables to markdown
    table_markdowns = []
    for table in tables:
        try:
            md = table.to_markdown()
            if md:
                table_markdowns.append(md)
        except Exception:
            pass

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
    enable_ocr = getattr(config, 'enable_ocr', True)
    
    for page_num in range(len(doc)):
        if progress_callback:
            progress_callback(page_num + 1, len(doc))
        page = doc.load_page(page_num)
        text = extract_page_text_and_tables(page, enable_ocr=enable_ocr)
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
