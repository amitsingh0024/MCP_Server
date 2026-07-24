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
        # With chunk_size=1000, the middle paragraph must split out as it exceeds the size
        chunks = chunk_document(pages, chunk_size=1000, chunk_overlap=100)
        self.assertTrue(len(chunks) >= 3)
        self.assertEqual(chunks[0]["page_start"], 1)
        self.assertEqual(chunks[1]["text_content"], "A" * 1500)

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

if __name__ == "__main__":
    unittest.main()
