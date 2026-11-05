import io
import os
import re
import time
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


# =====================================================================
# Indic OCR microservice (self-hosted Baidu Unlimited-OCR-2, LoRA fine-tuned)
# =====================================================================
# Configured by env rather than config.json: this is a deployment endpoint, not a per-org
# or per-document setting, and it differs between a laptop tunnel and a hosted GPU.
INDIC_URL = os.getenv("PDFSPECS_OCR_SERVICE_URL", "").strip().rstrip("/")
INDIC_KEY = os.getenv("PDFSPECS_OCR_SERVICE_KEY", "").strip()
# Render DPI for the page image sent to the model. 200 is what the model was validated at
# here; 300 (what Tesseract uses) triples the upload for no measured gain.
INDIC_DPI = int(os.getenv("PDFSPECS_OCR_SERVICE_DPI", "200"))
# Seconds per page. Measured ~77s per dense Devanagari page on a 6GB RTX 4050, so the
# default has to be generous or every page times out mid-generation.
INDIC_TIMEOUT = int(os.getenv("PDFSPECS_OCR_SERVICE_TIMEOUT", "300"))
# fast_mode defaults OFF, against the service's own default of on. Measured on a dense
# Sanskrit page: 75.9s with it vs 76.7s without — no speed gain, because latency is
# dominated by ~880 GENERATED tokens at ~11 tok/s, not by the visual tokens fast_mode
# trims. Accuracy, though, clearly degrades:
#   fast_mode=false  एकदेशोष्टितः शोफो ब्रणानां पूर्वलक्षणम् ॥   (correct)
#   fast_mode=true   एकदेशोचित: ओफो बशानां पूर्वनक्षामू ||       (garbled)
INDIC_FAST_MODE = os.getenv("PDFSPECS_OCR_SERVICE_FAST_MODE", "0").strip().lower() \
    in ("1", "true", "yes")
# Model checkpoint; empty uses whatever the service has loaded ("latest").
INDIC_CHECKPOINT = os.getenv("PDFSPECS_OCR_SERVICE_CHECKPOINT", "").strip()

# The service prefixes multi-page output with a page banner; strip it so it never reaches
# a chunk's text (and from there the tsvector and the embedding).
_PAGE_BANNER = re.compile(r"^#{1,6}\s*(?:\U0001F4C4|📄)?\s*Page\s+\d+\s*$", re.MULTILINE)


class IndicOCRServiceProvider(OCRProvider):
    """
    OCR via the self-hosted Indic OCR microservice (POST /v1/parse).

    Unlike Tesseract and PaddleOCR this is a vision-language model, which matters in two
    ways. It returns Markdown with tables already rendered as HTML, so the PyMuPDF table
    extraction the other providers append is NOT added here — it would duplicate content
    the model already laid out. And it is slow: roughly 77s per dense page, serialized on
    one GPU, so it is worth OCR'ing only the pages that actually need it (which is what
    parser's corruption check decides).
    """

    def __init__(self, base_url: str = None, api_key: str = None, dpi: int = None,
                 timeout: int = None, fast_mode: bool = None, checkpoint: str = None,
                 retries: int = 2):
        self.base_url = (base_url or INDIC_URL).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "PDFSPECS_OCR_SERVICE_URL is not set, so the 'indic' OCR provider has no "
                "endpoint to call (e.g. https://<host>, serving POST /v1/parse).")
        self.api_key = api_key if api_key is not None else INDIC_KEY
        self.dpi = dpi or INDIC_DPI
        self.timeout = timeout or INDIC_TIMEOUT
        self.fast_mode = INDIC_FAST_MODE if fast_mode is None else fast_mode
        self.checkpoint = checkpoint if checkpoint is not None else INDIC_CHECKPOINT
        self.retries = retries

        import requests
        self.requests = requests

        # Probe once at construction. get_ocr_provider degrades to Tesseract when a
        # provider can't be built, so an unreachable endpoint is caught here — once, loudly
        # — instead of failing every page of every document in turn.
        r = self.requests.get(f"{self.base_url}/healthz", timeout=30)
        r.raise_for_status()
        health = r.json()
        if not health.get("model_loaded"):
            raise RuntimeError(f"OCR service at {self.base_url} reports model_loaded=false")
        logger.info("Indic OCR service ready at %s (gpu=%s, dpi=%d, fast_mode=%s)",
                    self.base_url, health.get("gpu_name", "?"), self.dpi, self.fast_mode)

    def _headers(self) -> dict:
        # The service accepts either header and enforces neither when API_SECRET_KEY is
        # unset on its side, so an empty key is a valid local configuration.
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def ocr_page(self, page, tables) -> str:
        page_no = getattr(page, "number", "?")
        try:
            png = page.get_pixmap(dpi=self.dpi).tobytes("png")
        except Exception as e:
            logger.error("Could not rasterise page %s for OCR (%s: %s).",
                         page_no, type(e).__name__, e)
            return None

        data = {"pages": "1", "fast_mode": str(self.fast_mode).lower()}
        if self.checkpoint:
            data["checkpoint"] = self.checkpoint

        last = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.requests.post(
                    f"{self.base_url}/v1/parse",
                    files={"file": (f"page_{page_no}.png", png, "image/png")},
                    data=data, headers=self._headers(), timeout=self.timeout)
                # A 4xx is our mistake (bad key, bad form field) and will not improve on
                # retry; a 5xx or a dropped tunnel might.
                if 400 <= resp.status_code < 500:
                    logger.error("OCR service rejected page %s: %s %s",
                                 page_no, resp.status_code, resp.text[:200])
                    return None
                resp.raise_for_status()
                payload = resp.json()
                if not payload.get("success", True):
                    logger.error("OCR service reported failure on page %s: %s",
                                 page_no, str(payload)[:200])
                    return None
                text = _PAGE_BANNER.sub("", payload.get("markdown") or "").strip()
                if not text:
                    logger.error("OCR service returned no text for page %s; falling back "
                                 "to the text layer, which may be the corrupted one.", page_no)
                    return None
                return text
            except Exception as e:
                last = e
                if attempt < self.retries:
                    wait = 2 ** attempt
                    logger.warning("OCR service call failed for page %s (%s: %s); retrying "
                                   "in %ss (%d/%d).", page_no, type(e).__name__,
                                   str(e)[:120], wait, attempt + 1, self.retries)
                    time.sleep(wait)

        # Returning None means the caller keeps the text layer. For this provider that is
        # usually the corrupted layer we were trying to replace, so log at ERROR.
        logger.error("OCR service unreachable for page %s after %d attempts (%s: %s); "
                     "keeping the existing text layer.", page_no, self.retries + 1,
                     type(last).__name__ if last else "?", str(last)[:150])
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

    if provider_name not in ("paddle", "tesseract", "indic"):
        logger.warning("Unknown OCR provider %r; using Tesseract.", provider_name)
        return TesseractOCR(**kwargs)

    if provider_name == "tesseract":
        return TesseractOCR(**kwargs)

    if provider_name == "indic":
        try:
            return IndicOCRServiceProvider(**kwargs)
        except Exception as e:
            logger.error("Indic OCR service unavailable (%s: %s); falling back to "
                         "Tesseract, which produces markedly worse Devanagari. Check "
                         "PDFSPECS_OCR_SERVICE_URL and that the service is reachable.",
                         type(e).__name__, e)
            return TesseractOCR()

    try:
        return PaddleOCRProvider(**kwargs)
    except Exception as e:
        logger.error("PaddleOCR unavailable (%s: %s); falling back to Tesseract. "
                     "Set ocr_provider='tesseract' to silence this.",
                     type(e).__name__, e)
        return TesseractOCR()
