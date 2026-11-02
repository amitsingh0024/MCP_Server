import io
import logging
from abc import ABC, abstractmethod
from PIL import Image

logger = logging.getLogger("pdfspecs.ocr")

class OCRProvider(ABC):
    @abstractmethod
    def ocr_page(self, page, tables) -> str:
        """
        Extract text from a given page object (and optionally format detected tables).
        Returns None if OCR fails or is completely empty.
        """
        pass

class TesseractOCR(OCRProvider):
    def __init__(self, langs="eng+hin+san"):
        import pytesseract
        self.pytesseract = pytesseract
        self.langs = langs
        self._langs_checked = False

    def _check_ocr_languages(self):
        if self._langs_checked:
            return
        self._langs_checked = True
        try:
            available = set(self.pytesseract.get_languages(config=""))
            missing = [l for l in self.langs.split("+") if l not in available]
            if missing:
                logger.warning(
                    "Tesseract language pack(s) missing: %s. Scanned pages in those scripts will "
                    "OCR poorly or not at all. Install them (macOS: `brew install tesseract-lang`).",
                    ", ".join(missing),
                )
        except Exception as e:
            logger.warning("Could not enumerate tesseract languages: %s", e)

    def _tables_to_markdown(self, tables) -> list:
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

    def ocr_page(self, page, tables) -> str:
        self._check_ocr_languages()
        page_no = getattr(page, "number", "?")
        try:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            if img.mode != "RGB":
                img = img.convert("RGB")
            ocr_text = self.pytesseract.image_to_string(img, lang=self.langs).strip()
        except Exception as e:
            logger.warning("Tesseract OCR failed on page %s (%s: %s); falling back to text layer.",
                           page_no, type(e).__name__, e)
            return None

        if not ocr_text:
            logger.warning("Tesseract OCR produced no text on page %s; falling back to text layer.", page_no)
            return None

        table_markdowns = self._tables_to_markdown(tables)
        if table_markdowns:
            ocr_text += "\n\n### [Structured Table Data]\n" + "\n\n".join(table_markdowns)
        return ocr_text


class PaddleOCRProvider(OCRProvider):
    def __init__(self, lang="devanagari"):
        from paddleocr import PaddleOCR
        # Initialize PaddleOCR model.
        # Note: use_angle_cls=True helps with rotated text, show_log=False suppresses spam.
        self.ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
        self.lang = lang

    def _tables_to_markdown(self, tables) -> list:
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

    def ocr_page(self, page, tables) -> str:
        page_no = getattr(page, "number", "?")
        try:
            # We must pass an image array to PaddleOCR. We can extract it via PyMuPDF.
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            if img.mode != "RGB":
                img = img.convert("RGB")
            import numpy as np
            img_array = np.array(img)
            
            # Perform OCR.
            # Output is a list of lines, where each line is [bbox, (text, confidence)]
            result = self.ocr.ocr(img_array, cls=True)
            
            if not result or result[0] is None:
                logger.warning("PaddleOCR produced no text on page %s; falling back to text layer.", page_no)
                return None
                
            # Extract text from the result
            extracted_lines = []
            for line in result[0]:
                if len(line) >= 2 and len(line[1]) >= 1:
                    extracted_lines.append(line[1][0])
            
            ocr_text = "\n".join(extracted_lines).strip()
            
            if not ocr_text:
                logger.warning("PaddleOCR produced no text on page %s; falling back to text layer.", page_no)
                return None
                
            table_markdowns = self._tables_to_markdown(tables)
            if table_markdowns:
                ocr_text += "\n\n### [Structured Table Data]\n" + "\n\n".join(table_markdowns)
                
            return ocr_text
            
        except Exception as e:
            logger.warning("PaddleOCR failed on page %s (%s: %s); falling back to text layer.",
                           page_no, type(e).__name__, e)
            return None


def get_ocr_provider(provider_name: str, **kwargs) -> OCRProvider:
    """
    Build the configured OCR provider, degrading to Tesseract rather than raising.

    A provider that can't be constructed (missing package, unavailable model weights for
    the requested language) must not take the whole parse down — Tesseract is always
    installable and gives a usable, if weaker, result. The failure is logged at ERROR so
    it's visible: silently producing worse OCR is the failure mode this codebase has been
    bitten by before.
    """
    provider_name = (provider_name or "").lower().strip()

    if provider_name not in ("paddle", "tesseract"):
        logger.warning("Unknown OCR provider %r; using Tesseract.", provider_name)
        return TesseractOCR(**kwargs)

    if provider_name == "tesseract":
        return TesseractOCR(**kwargs)

    try:
        return PaddleOCRProvider(**kwargs)
    except Exception as e:
        logger.error("PaddleOCR unavailable (%s: %s); falling back to Tesseract. "
                     "Set ocr_provider='tesseract' to silence this.",
                     type(e).__name__, e)
        return TesseractOCR()
