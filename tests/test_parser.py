"""
Parser + OCR tests — the OCR decode path and its failure handling (REVAMP B-5).

The OCR implementation moved from src/parser.py into the provider classes in src/ocr.py
(Phase D), so the decode/failure tests target TesseractOCR directly. The page-isolation
and lazy-provider tests stay on the parser, which owns that policy.
"""
import logging
import unittest
from unittest import mock
from pathlib import Path

import fitz
from PIL import Image

from src import parser
from src import ocr as ocr_mod


def _pdf_page(text: str = "sample"):
    """Build a one-page in-memory PDF and return (doc, page)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc, page


class TestTesseractOcrDecode(unittest.TestCase):
    def setUp(self):
        self.provider = ocr_mod.TesseractOCR()

    def test_rgba_pixmap_is_decoded_instead_of_failing(self):
        """
        With an alpha-carrying pixmap, the old `Image.frombytes("RGB", ...)` did not raise
        — it read 4-byte RGBA samples as 3-byte RGB, shifting every pixel and handing
        tesseract a scrambled image. No exception, no log, just garbage OCR. Decoding via
        PNG lets PIL interpret the colorspace itself.
        """
        doc, page = _pdf_page()
        try:
            pix = page.get_pixmap(alpha=True)
            self.assertEqual(pix.n, 4, "expected an alpha-carrying pixmap for this test")
            # The old path's buffer-length mismatch, made explicit.
            self.assertNotEqual(len(pix.samples), pix.width * pix.height * 3)

            with mock.patch.object(page, "get_pixmap", return_value=pix), \
                 mock.patch.object(self.provider.pytesseract, "image_to_string",
                                   return_value="decoded text") as ocr:
                out = self.provider.ocr_page(page, tables=None)

            self.assertEqual(out, "decoded text")
            # OCR received a real RGB image, not a mangled buffer.
            img = ocr.call_args.args[0]
            self.assertIsInstance(img, Image.Image)
            self.assertEqual(img.mode, "RGB")
        finally:
            doc.close()

    def test_ocr_failure_is_logged_not_swallowed(self):
        """A failed OCR must leave a trace — on a scanned page it means a blank result."""
        doc, page = _pdf_page()
        try:
            with mock.patch.object(self.provider.pytesseract, "image_to_string",
                                   side_effect=RuntimeError("tesseract exploded")), \
                 self.assertLogs("pdfspecs.ocr", level=logging.WARNING) as logs:
                out = self.provider.ocr_page(page, tables=None)
            self.assertIsNone(out)
            self.assertTrue(any("OCR failed" in m for m in logs.output))
        finally:
            doc.close()

    def test_empty_ocr_result_is_logged(self):
        doc, page = _pdf_page()
        try:
            with mock.patch.object(self.provider.pytesseract, "image_to_string",
                                   return_value="   "), \
                 self.assertLogs("pdfspecs.ocr", level=logging.WARNING) as logs:
                out = self.provider.ocr_page(page, tables=None)
            self.assertIsNone(out)
            self.assertTrue(any("no text" in m for m in logs.output))
        finally:
            doc.close()


class TestCorruptionDetection(unittest.TestCase):
    """
    The OCR trigger used to ask only 'is the text layer thin?'. These PDFs embed subset
    fonts with broken ToUnicode maps, so the layer is LONG and wrong — 69% of the corpus.
    """

    def test_clean_english_is_not_flagged(self):
        self.assertFalse(parser.looks_corrupted(
            "Now we shall discourse on the chapter which treats of the medical "
            "treatment of the diseases peculiar to the ear."))

    def test_clean_devanagari_is_not_flagged(self):
        self.assertFalse(parser.looks_corrupted(
            "तस्मादव्यक्तान्महानुत्पद्यते तल्लिङ्ग एव | तल्लिङ्गाच्च महतस्तल्लक्षण एवाहङ्कार उत्पद्यते"))

    def test_mixed_script_with_whitespace_is_not_flagged(self):
        # Legitimate bilingual text separates scripts; only direct adjacency is a defect.
        self.assertFalse(parser.looks_corrupted(
            "Sanskrit: > अथातः कर्णगतरोग | Meaning: Now we shall discourse"))

    def test_real_mojibake_is_flagged(self):
        # Verbatim from sushruta_part01.pdf page 48 — Devanagari spliced with ASCII.
        self.assertTrue(parser.looks_corrupted(
            "तB समासेना'टिवधा मूढगभ7गितdिI'टा; वभावगता अिप Bयः सङ्गा भवि$त"))

    def test_control_bytes_are_flagged(self):
        # Raw C0 bytes are the signature of an absent/broken ToUnicode CMap.
        self.assertTrue(parser.looks_corrupted("त\x14माद\x12यEता$महानु\x1eपCते ति*लङ्ग एव"))

    def test_mismapped_foreign_script_is_flagged(self):
        """
        The regression that mattered: a broken ToUnicode table maps glyphs to valid
        codepoints in the WRONG script, so the text is structurally perfect — no control
        bytes, no private-use area, no U+FFFD, no Devanagari-beside-ASCII — and scored 0.
        OCR therefore never ran, and 89% of the Yogaratnakara test ingest was stored with
        mangled Sanskrit. U+077B here stands in for `वि`.
        """
        corrupted = "ݻवरुद्धाहारचेष्टस्य मन्दाग्नेݻनर्वश्चलस्य च"
        self.assertTrue(parser.looks_corrupted(corrupted))
        self.assertGreaterEqual(parser.corruption_score(corrupted), 2)

    def test_genuinely_multilingual_text_is_not_flagged(self):
        """
        Presence of another script is not corruption — adjacency is. Scripts separated by
        whitespace are how real multilingual text reads, so this must stay clean or every
        such page pays for a needless OCR pass.
        """
        self.assertFalse(parser.looks_corrupted("ज्वरचिकित्सितं العربية ज्वरः"))
        self.assertFalse(parser.looks_corrupted("fever ज्वरः Ελληνικά"))

    def test_single_stray_signal_does_not_trigger(self):
        """One odd character mustn't cost a ~5s OCR pass on an otherwise clean page."""
        self.assertEqual(parser.corruption_score("a normal page\x01"), 1)
        self.assertFalse(parser.looks_corrupted("a normal page\x01"))

    def test_empty_text_scores_zero(self):
        self.assertEqual(parser.corruption_score(""), 0)
        self.assertFalse(parser.looks_corrupted(""))


class TestOcrTrigger(unittest.TestCase):
    def test_corrupted_layer_triggers_ocr_in_auto_mode(self):
        """The regression that mattered: long-but-wrong text used to skip OCR entirely."""
        doc, page = _pdf_page("x" * 200)
        try:
            corrupt = "तB समासेना'टिवधा मूढगभ7गितdिI'टा" * 5
            provider = mock.Mock()
            provider.ocr_page.return_value = "clean devanagari"
            with mock.patch.object(page, "get_text", return_value=corrupt):
                out = parser.extract_page_text_and_tables(
                    page, tables=None, ocr_provider=lambda: provider, ocr_mode="auto")
            provider.ocr_page.assert_called_once()
            self.assertEqual(out, "clean devanagari")
        finally:
            doc.close()

    def test_clean_long_layer_does_not_trigger_ocr(self):
        doc, page = _pdf_page()
        try:
            clean = "Now we shall discourse on the treatment of fever. " * 5
            provider = mock.Mock()
            with mock.patch.object(page, "get_text", return_value=clean):
                parser.extract_page_text_and_tables(
                    page, tables=None, ocr_provider=lambda: provider, ocr_mode="auto")
            provider.ocr_page.assert_not_called()
        finally:
            doc.close()

    def test_never_mode_ignores_corruption(self):
        doc, page = _pdf_page()
        try:
            provider = mock.Mock()
            with mock.patch.object(page, "get_text", return_value="तB मूढगभ7गितdिI'टा" * 5):
                parser.extract_page_text_and_tables(
                    page, tables=None, ocr_provider=lambda: provider, ocr_mode="never")
            provider.ocr_page.assert_not_called()
        finally:
            doc.close()


class TestProviderSelection(unittest.TestCase):
    def test_unavailable_paddle_degrades_to_tesseract(self):
        """
        A provider that can't be constructed must not take the whole parse down. On this
        machine PaddleOCR 3.x has no model for lang='devanagari' and raises — that used to
        propagate out of parse_pdf and fail every document, text-layer PDFs included.
        """
        with mock.patch.object(ocr_mod, "PaddleOCRProvider",
                               side_effect=ValueError("No models available")), \
             self.assertLogs("pdfspecs.ocr", level=logging.ERROR) as logs:
            provider = ocr_mod.get_ocr_provider("paddle")
        self.assertIsInstance(provider, ocr_mod.TesseractOCR)
        self.assertTrue(any("falling back to Tesseract" in m for m in logs.output))

    def test_unknown_provider_name_uses_tesseract(self):
        with self.assertLogs("pdfspecs.ocr", level=logging.WARNING):
            self.assertIsInstance(ocr_mod.get_ocr_provider("nonsense"), ocr_mod.TesseractOCR)


class TestLazyOcrProvider(unittest.TestCase):
    def test_text_layer_document_never_builds_an_ocr_provider(self):
        """
        A PDF whose pages all have a text layer must not construct an OCR provider at all
        — loading a model costs seconds per document, and if construction fails it used to
        abort the parse of a document that never needed OCR in the first place.
        """
        doc = fitz.open()
        for i in range(3):
            # Comfortably longer than the default ocr_min_chars=40, or `auto` mode would
            # classify these as scanned pages and reach for OCR.
            doc.new_page().insert_text(
                (72, 72), f"page {i + 1} has a real and reasonably long text layer on it")
        path = Path("/tmp/_pdfspecs_test_lazy.pdf")
        doc.save(str(path))
        doc.close()

        with mock.patch("src.ocr.get_ocr_provider") as factory:
            result = parser.parse_pdf(path)

        factory.assert_not_called()
        self.assertEqual(len(result["pages"]), 3)
        self.assertIn("page 1", result["pages"][0]["text"])


class TestPageIsolation(unittest.TestCase):
    def test_one_bad_page_does_not_abort_the_document(self):
        """A single unparseable page used to lose every other page with it."""
        doc = fitz.open()
        for i in range(3):
            doc.new_page().insert_text(
                (72, 72), f"page {i + 1} body text, long enough to skip the OCR threshold")
        path = Path("/tmp/_pdfspecs_test_isolation.pdf")
        doc.save(str(path))
        doc.close()

        real = parser.extract_page_text_and_tables

        # `tables` is passed positionally by parse_pdf — match the real signature.
        def flaky(page, tables=None, **kwargs):
            if page.number == 1:          # second page blows up
                raise RuntimeError("corrupt page")
            return real(page, tables, **kwargs)

        with mock.patch.object(parser, "extract_page_text_and_tables", side_effect=flaky), \
             self.assertLogs("pdfspecs.parser", level=logging.WARNING):
            result = parser.parse_pdf(path)

        self.assertEqual(len(result["pages"]), 3)
        self.assertEqual(result["pages"][1]["text"], "")
        self.assertIn("page 1", result["pages"][0]["text"])
        self.assertIn("page 3", result["pages"][2]["text"])


if __name__ == "__main__":
    unittest.main()


class TestParseMarkdown(unittest.TestCase):
    """
    parse_markdown must return exactly what parse_pdf returns, because ingest.process_task
    consumes the two interchangeably and the chunker reads `pages` positionally.
    """

    BOOK = (
        "# Sushruta\n\n"
        "Front matter that belongs with the first record.\n\n"
        "### Annapana vidhi — Shloka 292.0\n\n"
        "**Sanskrit:** वेणोः करीराः कफला मधुरा रसपाकतः\n\n"
        "**Meaning:** The tender sprouts of Venu generate Kapha.\n\n"
        "### Annapana vidhi — Shloka 293.0\n\n"
        "**Sanskrit:** उत्पलादिकषायाद्याश्चन्दनोशीरसंयुताः\n\n"
        "**Meaning:** Cooling and sweet, they subdue fever.\n"
    )

    def _write(self, text, name="book.md"):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="pdfspecs-md-"))
        p = d / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_returns_the_same_keys_as_parse_pdf(self):
        out = parser.parse_markdown(self._write(self.BOOK))
        self.assertEqual(set(out), {"sha256", "filename", "size_bytes", "pages"})
        self.assertEqual(out["filename"], "book.md")
        self.assertEqual(len(out["sha256"]), 64)
        self.assertTrue(all({"page_num", "text"} == set(p) for p in out["pages"]))

    def test_one_page_per_record_and_front_matter_joins_the_first(self):
        out = parser.parse_markdown(self._write(self.BOOK))
        self.assertEqual([p["page_num"] for p in out["pages"]], [1, 2])
        # Front matter must not become a page of its own, or every page_map key would be
        # off by one against the record index it is supposed to address.
        self.assertIn("Front matter", out["pages"][0]["text"])
        self.assertIn("Shloka 292.0", out["pages"][0]["text"])
        self.assertIn("Shloka 293.0", out["pages"][1]["text"])

    def test_page_map_overrides_the_ordinal(self):
        out = parser.parse_markdown(self._write(self.BOOK), page_map={0: 17, 1: 17})
        # Both records sit on printed page 17; repeated page numbers are expected and the
        # chunker's offset table handles them.
        self.assertEqual([p["page_num"] for p in out["pages"]], [17, 17])

    def test_chunker_consumes_the_output_and_keeps_citations(self):
        from src import chunker
        out = parser.parse_markdown(self._write(self.BOOK), page_map={0: 17, 1: 18})
        chunks = chunker.chunk_document(out["pages"], 2500, 250)
        self.assertTrue(chunks)
        self.assertTrue(all(c["page_start"] in (17, 18) for c in chunks))
        # The verse reference travels inside the text, which is the real citation.
        self.assertTrue(any("Shloka 292.0" in c["text_content"] for c in chunks))

    def test_file_without_records_still_parses_as_one_page(self):
        with self.assertLogs("pdfspecs.parser", level="WARNING"):
            out = parser.parse_markdown(self._write("Just prose, no records here.\n"))
        self.assertEqual(len(out["pages"]), 1)
        self.assertEqual(out["pages"][0]["page_num"], 1)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            parser.parse_markdown(Path("/nonexistent/none.md"))

    def test_progress_callback_is_throttled(self):
        many = "".join(f"### Entry — Shloka {i}.0\n\nBody text {i}\n\n" for i in range(500))
        calls = []
        parser.parse_markdown(self._write(many),
                              progress_callback=lambda c, t: calls.append((c, t)))
        # Each call writes progress to the database in ingest, so 500 records must not
        # produce 500 writes.
        self.assertLessEqual(len(calls), parser._MD_PROGRESS_STEPS + 2)
        self.assertEqual(calls[-1][0], calls[-1][1])
