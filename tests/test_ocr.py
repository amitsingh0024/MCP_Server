"""
Tests for the Indic OCR microservice provider (self-hosted Baidu Unlimited-OCR-2).

This provider differs from Tesseract and PaddleOCR in ways that are easy to regress:

* It is a vision-language model that returns Markdown with tables ALREADY rendered as
  HTML, so the PyMuPDF table extraction the other two append must not be added — doing so
  duplicates content the model already laid out.
* It is remote and slow (~77s per dense page), so a dropped tunnel is an expected event,
  not an exception: 5xx and connection failures retry, 4xx does not.
* When it fails, the caller keeps the existing text layer — which for this corpus is the
  corrupted one we were trying to replace. That must be loud, never silent.
"""
import unittest
from unittest import mock

from src import ocr


def _page(number=18, png=b"\x89PNG-fake"):
    page = mock.Mock()
    page.number = number
    page.get_pixmap.return_value.tobytes.return_value = png
    return page


def _healthz(model_loaded=True):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = {"status": "healthy", "model_loaded": model_loaded,
                           "gpu_name": "NVIDIA GeForce RTX 4050 Laptop GPU"}
    r.raise_for_status.return_value = None
    return r


def _parsed(markdown, success=True, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = {"success": success, "markdown": markdown,
                           "latency_sec": 76.7, "tokens": 884}
    r.raise_for_status.return_value = None
    return r


URL = "https://ocr.example.test"


class TestConstruction(unittest.TestCase):
    def test_missing_url_is_refused(self):
        """Without an endpoint the provider cannot work; say so instead of failing later."""
        with mock.patch.object(ocr, "INDIC_URL", ""):
            with self.assertRaises(ValueError) as ctx:
                ocr.IndicOCRServiceProvider()
        self.assertIn("PDFSPECS_OCR_SERVICE_URL", str(ctx.exception))

    def test_health_is_probed_once_at_construction(self):
        """
        An unreachable endpoint must fail at construction, where get_ocr_provider can
        degrade once and loudly, rather than failing every page of every document.
        """
        with mock.patch("requests.get", return_value=_healthz()) as get:
            ocr.IndicOCRServiceProvider(base_url=URL)
        get.assert_called_once()
        self.assertIn("/healthz", get.call_args[0][0])

    def test_unloaded_model_is_refused(self):
        with mock.patch("requests.get", return_value=_healthz(model_loaded=False)):
            with self.assertRaises(RuntimeError):
                ocr.IndicOCRServiceProvider(base_url=URL)

    def test_factory_degrades_to_tesseract_when_unreachable(self):
        """A dead tunnel must not take the whole parse down."""
        with mock.patch("requests.get", side_effect=OSError("tunnel is gone")):
            provider = ocr.get_ocr_provider("indic")
        self.assertIsInstance(provider, ocr.TesseractOCR)

    def test_factory_builds_the_provider_when_healthy(self):
        with mock.patch("requests.get", return_value=_healthz()):
            provider = ocr.get_ocr_provider("indic", base_url=URL)
        self.assertIsInstance(provider, ocr.IndicOCRServiceProvider)


class TestProviderSelectionDoesNotDrift(unittest.TestCase):
    """
    config.Config and ocr.get_ocr_provider each validate the provider name, so the two
    lists must agree.

    The regression that mattered: config's list was ("paddle", "tesseract"), so
    PDFSPECS_OCR_PROVIDER=indic was silently rewritten to "paddle" — which has no
    Devanagari models and degrades to Tesseract. The run would have used the wrong engine,
    at a fifteenth of the accuracy, with nothing in the logs to say why.
    """

    def test_config_accepts_every_provider_ocr_can_build(self):
        import os
        from src import config as cfg
        for name in cfg.OCR_PROVIDERS:
            with mock.patch.dict(os.environ, {"PDFSPECS_OCR_PROVIDER": name}):
                self.assertEqual(cfg.Config().ocr_provider, name)

    def test_indic_is_a_known_provider(self):
        from src import config as cfg
        self.assertIn("indic", cfg.OCR_PROVIDERS)

    def test_unknown_provider_falls_back_without_pretending(self):
        import os
        from src import config as cfg
        with mock.patch.dict(os.environ, {"PDFSPECS_OCR_PROVIDER": "nonesuch"}):
            self.assertEqual(cfg.Config().ocr_provider, "tesseract")


class TestOcrPage(unittest.TestCase):
    def setUp(self):
        with mock.patch("requests.get", return_value=_healthz()):
            self.provider = ocr.IndicOCRServiceProvider(base_url=URL, retries=1)

    def test_markdown_is_returned(self):
        md = "## Vranasotha\n\nएकदेशोष्टितः शोफो ब्रणानां पूर्वलक्षणम् ॥"
        with mock.patch("requests.post", return_value=_parsed(md)):
            self.assertEqual(self.provider.ocr_page(_page(), None), md)

    def test_page_banner_is_stripped(self):
        """The banner would otherwise land in the chunk text, tsvector and embedding."""
        with mock.patch("requests.post", return_value=_parsed("# 📄 Page 1\n\nreal text")):
            self.assertEqual(self.provider.ocr_page(_page(), None), "real text")

    def test_pymupdf_tables_are_not_appended(self):
        """
        The model already emits tables as HTML. Appending PyMuPDF's extraction — which the
        Tesseract and Paddle providers do — would duplicate the same table twice.
        """
        tables = mock.Mock()
        tables.tables = [mock.Mock()]
        with mock.patch("requests.post", return_value=_parsed("<table><tr><td>a</td></tr></table>")):
            out = self.provider.ocr_page(_page(), tables)
        self.assertIn("<table>", out)
        self.assertNotIn("[Structured Table Data]", out)

    def test_empty_markdown_falls_back(self):
        with mock.patch("requests.post", return_value=_parsed("   ")):
            self.assertIsNone(self.provider.ocr_page(_page(), None))

    def test_success_false_falls_back(self):
        with mock.patch("requests.post", return_value=_parsed("x", success=False)):
            self.assertIsNone(self.provider.ocr_page(_page(), None))

    def test_client_error_is_not_retried(self):
        """A 4xx is our own bad request and cannot improve on retry."""
        bad = mock.Mock()
        bad.status_code = 401
        bad.text = "unauthorized"
        with mock.patch("requests.post", return_value=bad) as post, \
             mock.patch("time.sleep"):
            self.assertIsNone(self.provider.ocr_page(_page(), None))
        self.assertEqual(post.call_count, 1)

    def test_transient_failure_is_retried_then_gives_up(self):
        """A dropped tunnel is expected; retry, then keep the text layer."""
        with mock.patch("requests.post", side_effect=OSError("connection reset")) as post, \
             mock.patch("time.sleep"):
            self.assertIsNone(self.provider.ocr_page(_page(), None))
        self.assertEqual(post.call_count, 2)          # retries=1 -> two attempts

    def test_retry_succeeds_on_the_second_attempt(self):
        with mock.patch("requests.post",
                        side_effect=[OSError("reset"), _parsed("recovered")]), \
             mock.patch("time.sleep"):
            self.assertEqual(self.provider.ocr_page(_page(), None), "recovered")

    def test_fast_mode_is_off_by_default_in_the_request(self):
        """
        Measured: fast_mode gives no speedup here (latency is generation-bound) and
        visibly degrades Devanagari, so the request must not quietly enable it.
        """
        with mock.patch.object(ocr, "INDIC_FAST_MODE", False), \
             mock.patch("requests.get", return_value=_healthz()):
            provider = ocr.IndicOCRServiceProvider(base_url=URL)
        with mock.patch("requests.post", return_value=_parsed("x")) as post:
            provider.ocr_page(_page(), None)
        self.assertEqual(post.call_args.kwargs["data"]["fast_mode"], "false")

    def test_api_key_is_sent_when_configured(self):
        with mock.patch("requests.get", return_value=_healthz()):
            provider = ocr.IndicOCRServiceProvider(base_url=URL, api_key="ocr_secret")
        with mock.patch("requests.post", return_value=_parsed("x")) as post:
            provider.ocr_page(_page(), None)
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-Key"], "ocr_secret")

    def test_no_auth_header_without_a_key(self):
        """The service enforces no auth when API_SECRET_KEY is unset on its side."""
        with mock.patch("requests.post", return_value=_parsed("x")) as post:
            self.provider.ocr_page(_page(), None)
        self.assertEqual(post.call_args.kwargs["headers"], {})


if __name__ == "__main__":
    unittest.main()
