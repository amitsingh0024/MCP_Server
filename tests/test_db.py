import unittest
import os
from pathlib import Path
from unittest.mock import patch

# Set a test database path
TEST_DB_PATH = Path("/Volumes/Untitled/proj/work_stuff/test_data.db")

class MockConfig:
    db_path = TEST_DB_PATH

# Import database module and config to patch
import src.config
import src.db as db

class TestDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Patch configuration db_path before initializing db
        cls.patcher = patch("src.db.get_config")
        cls.mock_get_config = cls.patcher.start()
        cls.mock_get_config.return_value = MockConfig()
        
        # Initialize test DB tables
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        if TEST_DB_PATH.exists():
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def setUp(self):
        # Clear database before each test
        with db.get_connection() as conn:
            conn.execute("DELETE FROM chunk_entities")
            conn.execute("DELETE FROM chunk_keywords")
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM documents")
            try:
                conn.execute("DELETE FROM chunks_fts")
            except Exception:
                pass

    def test_document_crud(self):
        doc_id = "test-doc-id"
        db.add_document(doc_id, "test.pdf", "hash123", 1024)
        
        # Retrieve by hash
        doc = db.get_document_by_sha256("hash123")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["filename"], "test.pdf")
        
        # List all
        docs = db.list_documents()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], doc_id)
        
        # Delete doc
        db.delete_document(doc_id)
        self.assertIsNone(db.get_document_by_sha256("hash123"))

    def test_chunks_and_relations(self):
        doc_id = "doc-id-2"
        chunk_id = "chunk-id-1"
        db.add_document(doc_id, "doc2.pdf", "hash456", 2048)
        
        # Mock vector embedding
        mock_embedding = [0.1, -0.2, 0.5, 0.9]
        db.add_chunk(
            chunk_id=chunk_id,
            document_id=doc_id,
            page_start=2,
            page_end=3,
            text_content="This is some test content mentioning Gemini API.",
            summary="Gemini test chunk",
            embedding=mock_embedding
        )
        
        # Verify chunk retrieval
        chunks = db.get_chunks_for_document(doc_id)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["id"], chunk_id)
        self.assertEqual(chunks[0]["page_start"], 2)
        self.assertEqual(chunks[0]["page_end"], 3)
        self.assertEqual(chunks[0]["text_content"], "This is some test content mentioning Gemini API.")
        self.assertEqual(chunks[0]["summary"], "Gemini test chunk")
        
        # Verify embedding deserialization
        deserialized = list(chunks[0]["embedding"])
        for a, b in zip(deserialized, mock_embedding):
            self.assertAlmostEqual(a, b, places=5)
            
        # Verify FTS search index
        hits = db.query_lexical_fts("Gemini")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["chunk_id"], chunk_id)
        
        # Verify Entity graph additions and linking
        db.add_entity("concept:gemini-api", "Gemini API", "concept")
        db.link_chunk_to_entity(chunk_id, "concept:gemini-api")
        db.add_keyword(chunk_id, "test")
        
        # Verify graph database relationships
        with db.get_connection() as conn:
            # Check entities
            ent = conn.execute("SELECT * FROM entities WHERE id = 'concept:gemini-api'").fetchone()
            self.assertIsNotNone(ent)
            self.assertEqual(ent["name"], "Gemini API")
            
            # Check edge linkages
            link = conn.execute("SELECT * FROM chunk_entities WHERE chunk_id = ? AND entity_id = ?", (chunk_id, "concept:gemini-api")).fetchone()
            self.assertIsNotNone(link)
            
            keyword = conn.execute("SELECT * FROM chunk_keywords WHERE chunk_id = ? AND keyword = ?", (chunk_id, "test")).fetchone()
            self.assertIsNotNone(keyword)

if __name__ == "__main__":
    unittest.main()
