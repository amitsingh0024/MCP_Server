import unittest
from src.chunker import chunk_document

class TestChunker(unittest.TestCase):
    def test_empty_pages(self):
        self.assertEqual(chunk_document([]), [])

    def test_basic_chunking(self):
        pages = [
            {"page_num": 1, "text": "Paragraph 1.1\n\nParagraph 1.2"},
            {"page_num": 2, "text": "Paragraph 2.1\n\nParagraph 2.2"}
        ]
        # Large chunk size should result in a single chunk
        chunks = chunk_document(pages, chunk_size=5000, chunk_overlap=100)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["page_start"], 1)
        self.assertEqual(chunks[0]["page_end"], 2)
        self.assertTrue("Paragraph 1.1" in chunks[0]["text_content"])
        self.assertTrue("Paragraph 2.2" in chunks[0]["text_content"])

    def test_chunk_splitting(self):
        pages = [
            {"page_num": 1, "text": "Short paragraph 1.\n\n" + "A" * 1500 + "\n\nShort paragraph 2."}
        ]
        # With chunk_size=1000, the middle paragraph must split out as it exceeds the size.
        # It is now SPLIT rather than emitted whole (REVAMP B-3), so no chunk may exceed
        # chunk_size — previously chunks[1] was the full 1500-char blob.
        chunks = chunk_document(pages, chunk_size=1000, chunk_overlap=100)
        self.assertTrue(len(chunks) >= 3)
        self.assertEqual(chunks[0]["page_start"], 1)
        for c in chunks:
            self.assertLessEqual(len(c["text_content"]), 1000)
        # No content is lost in the split (>= because overlap may repeat some).
        self.assertGreaterEqual("".join(c["text_content"] for c in chunks).count("A"), 1500)

    def test_oversized_paragraph_splits_on_devanagari_danda(self):
        # A Sanskrit passage contains no ASCII sentence terminators — only the danda `।`.
        # An ASCII-only boundary pattern would find none and split blindly mid-word.
        sloka = "अथातो ज्वरचिकित्सितं व्याख्यास्यामः। "
        pages = [{"page_num": 1, "text": sloka * 40}]
        chunks = chunk_document(pages, chunk_size=300, chunk_overlap=50)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["text_content"]), 300)
        # Boundaries land after a danda, so no chunk starts mid-word.
        self.assertTrue(all(c["text_content"].startswith("अथातो") for c in chunks))

    def test_oversized_paragraph_without_any_boundary(self):
        # No sentence terminators at all: must still bound every chunk by chunk_size.
        pages = [{"page_num": 1, "text": "x" * 5000}]
        chunks = chunk_document(pages, chunk_size=400, chunk_overlap=40)
        self.assertGreaterEqual(len(chunks), 13)
        for c in chunks:
            self.assertLessEqual(len(c["text_content"]), 400)

    def test_overlap_carryover(self):
        pages = [
            {"page_num": 1, "text": "Para A\n\nPara B\n\nPara C"}
        ]
        # Force split. Chunk size 15, overlap 10.
        # "Para A" (6 chars) + "Para B" (6 chars) = 14 chars. Fits in 15.
        # "Para C" (6 chars) exceeds. Next chunk starts.
        # Overlap of 10 allows "Para B" (6 chars) to carry over.
        chunks = chunk_document(pages, chunk_size=15, chunk_overlap=10)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["text_content"], "Para A\n\nPara B")
        self.assertEqual(chunks[1]["text_content"], "Para B\n\nPara C")

class TestRecordChunking(unittest.TestCase):
    """
    These texts are a structured export (7,999 shloka headers vs 7,999 `Sanskrit:` labels
    across 1,974 pages), so chunks follow record boundaries rather than a character budget.
    """

    def _pages(self):
        # A record deliberately straddling the page break: shloka ends p1, Meaning on p2 —
        # exactly the sushruta_part01 p130/p131 case.
        return [
            {"page_num": 1, "text":
                "Vrana (Ulcers) — Shloka 1.0\nSutra Sthana | Chapter 1\n"
                "Sanskrit: > अथातो व्रणचिकित्सितं व्याख्यास्यामः॥\n"
                "Meaning: Now we shall discourse on the treatment of ulcers.\n"
                "Yantra (Surgical instruments) — Shloka 5.0\nSutra Sthana | Chapter 7\n"
                "Sanskrit: > तत्र, नानाप्रकाराणां व्यालानां मृगपक्षिणां मुखैर्मुखानि॥"},
            {"page_num": 2, "text":
                "Meaning: The mouths of these appliances resemble those of birds and beasts.\n"
                "Description: Yantra classification\n"
                "Jwara (Fever) — Shloka 2.0\nNidana Sthana | Chapter 1\n"
                "Sanskrit: > ज्वरो रोगपतिः पाप्मा मृत्युरोजोऽशनोऽन्तकः॥\n"
                "Meaning: Fever is the lord of diseases."},
        ]

    def test_record_split_across_pages_stays_one_chunk(self):
        chunks = chunk_document(self._pages(), chunk_size=2500, chunk_overlap=250)
        yantra = [c for c in chunks if "Yantra" in c["text_content"]]
        self.assertEqual(len(yantra), 1, "the Yantra record must not be split by the page break")
        body = yantra[0]["text_content"]
        # Verse and its translation must travel together — separating them was the bug.
        self.assertIn("नानाप्रकाराणां", body)
        self.assertIn("mouths of these appliances", body)
        self.assertEqual(yantra[0]["page_start"], 1)
        self.assertEqual(yantra[0]["page_end"], 2)

    def test_one_chunk_per_record(self):
        chunks = chunk_document(self._pages(), chunk_size=2500, chunk_overlap=250)
        self.assertEqual(len(chunks), 3)          # Vrana, Yantra, Jwara
        for c in chunks:
            self.assertIn("Sanskrit:", c["text_content"])
            self.assertIn("Meaning:", c["text_content"])

    def test_pages_joined_mid_sentence_are_reassembled(self):
        """A page ending mid-sentence continues on the next; the join must not break it."""
        pages = [
            {"page_num": 1, "text": "Netra — Shloka 1.0\nSanskrit: > अथातः॥\n"
                                    "Meaning: measures should be prescribed according to the"},
            {"page_num": 2, "text": "deranged Dosa involved in each case."},
        ]
        chunks = chunk_document(pages, chunk_size=2500, chunk_overlap=250)
        joined = " ".join(c["text_content"] for c in chunks)
        self.assertIn("according to the deranged Dosa involved in each case.", joined)

    def test_header_only_record_is_dropped(self):
        pages = [{"page_num": 1, "text":
                  "Entry — Shloka 2.0\n"
                  "Jwara (Fever) — Shloka 3.0\nSanskrit: > ज्वरो रोगपतिः॥\n"
                  "Meaning: Fever is the lord of diseases and must be treated first."}]
        chunks = chunk_document(pages, chunk_size=2500, chunk_overlap=250)
        self.assertEqual(len(chunks), 1)
        self.assertIn("Jwara", chunks[0]["text_content"])

    def test_unstructured_document_falls_back_to_paragraphs(self):
        """Documents without shloka headers must still chunk the old way."""
        pages = [{"page_num": 1, "text": "Plain paragraph one.\n\nPlain paragraph two."}]
        chunks = chunk_document(pages, chunk_size=5000, chunk_overlap=100)
        self.assertEqual(len(chunks), 1)
        self.assertIn("Plain paragraph one.", chunks[0]["text_content"])


if __name__ == "__main__":
    unittest.main()
