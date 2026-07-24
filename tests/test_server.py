import unittest
import os
from pathlib import Path
from unittest.mock import patch

# Set a test database path for the API server
TEST_SERVER_DB_PATH = Path("/Volumes/Untitled/proj/work_stuff/test_server_data.db")

class MockConfig:
    db_path = TEST_SERVER_DB_PATH
    provider = "gemini"
    gemini_api_key = ""
    nvidia_api_key = ""
    llm_model = "gemini-1.5-flash"
    embedding_model = "text-embedding-004"
    chunk_size = 2500
    chunk_overlap = 250
    
    def load(self):
        import sqlite3
        if not TEST_SERVER_DB_PATH.exists():
            return
        conn = sqlite3.connect(TEST_SERVER_DB_PATH)
        cursor = conn.cursor()
        try:
            row = cursor.execute("SELECT value FROM settings WHERE key = 'provider'").fetchone()
            if row: self.provider = row[0]
            
            row = cursor.execute("SELECT value FROM settings WHERE key = 'gemini_api_key'").fetchone()
            if row:
                from src.crypto_utils import decrypt_val
                self.gemini_api_key = decrypt_val(row[0])
        except Exception:
            pass
        conn.close()

class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import src.server first so it's loaded in sys.modules before patching
        import src.server
        
        # Patch configurations globally for the duration of this test class
        cls.cfg_patcher = patch("src.config.get_config")
        cls.db_cfg_patcher = patch("src.db.get_config")
        cls.server_cfg_patcher = patch("src.server.get_config")
        
        cls.mock_cfg = cls.cfg_patcher.start()
        cls.mock_db_cfg = cls.db_cfg_patcher.start()
        cls.mock_server_cfg = cls.server_cfg_patcher.start()
        
        cls.mock_cfg.return_value = MockConfig()
        cls.mock_db_cfg.return_value = MockConfig()
        cls.mock_server_cfg.return_value = MockConfig()
        
        # Import and initialize DB
        import src.db as db
        db.init_db()
        
        # Import and initialize TestClient
        from fastapi.testclient import TestClient
        from src.server import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.cfg_patcher.stop()
        cls.db_cfg_patcher.stop()
        cls.server_cfg_patcher.stop()
        
        if TEST_SERVER_DB_PATH.exists():
            try:
                os.remove(TEST_SERVER_DB_PATH)
            except Exception:
                pass

    def setUp(self):
        import src.db as db
        # Clear DB tables before each test
        with db.get_connection() as conn:
            conn.execute("DELETE FROM api_keys")
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM documents")
            conn.execute("DELETE FROM tasks")

    def test_settings_bootstrapping_and_encryption(self):
        import src.db as db
        # 1. Verify bootstrapping state (no keys exist, so auth is disabled)
        res = self.client.get("/api/v1/config/provider")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["auth_required"], False)
        self.assertEqual(data["has_gemini_key"], False)

        # 2. Save settings (Gemini API key should be saved encrypted)
        payload = {
            "provider": "gemini",
            "gemini_api_key": "super-secret-key-123",
            "llm_model": "gemini-1.5-flash",
            "chunk_size": 2000
        }
        res = self.client.post("/api/v1/config", json=payload)
        self.assertEqual(res.status_code, 200)

        # 3. Verify settings are saved in DB
        plain_key = db.get_setting("gemini_api_key", decrypt=True)
        self.assertEqual(plain_key, "super-secret-key-123")
        
        # Verify it is encrypted in raw DB row
        with db.get_connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'gemini_api_key'").fetchone()
            self.assertIsNotNone(row)
            self.assertNotEqual(row["value"], "super-secret-key-123")

    def test_token_auth_middleware(self):
        import src.db as db
        # 1. Before generating any key, public endpoints and protected endpoints should be accessible (auth disabled)
        res = self.client.get("/api/v1/documents")
        self.assertEqual(res.status_code, 200)

        # 2. Generate the first Token 1 via the API (succeeds because auth is still disabled)
        res1 = self.client.post("/api/v1/auth/tokens", json={"description": "Token 1"})
        self.assertEqual(res1.status_code, 200)
        token1 = res1.json()["token"]

        # Insert Token 2 directly into the database to bypass API auth blocking
        token2 = "sk-pdfspecs-test-token-2-key"
        db.add_api_key(token2, "Token 2")

        # 3. Verify that auth is now required (auth_required = True)
        res = self.client.get("/api/v1/config/provider")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["auth_required"], True)

        # 4. Accessing protected endpoint without token should now fail with 401
        res = self.client.get("/api/v1/documents")
        self.assertEqual(res.status_code, 401)
        self.assertIn("Unauthorized", res.json()["detail"])

        # 5. Accessing with invalid token should fail with 401
        res = self.client.get("/api/v1/documents", headers={"Authorization": "Bearer sk-invalid-token"})
        self.assertEqual(res.status_code, 401)

        # 6. Accessing with valid Token 1 should succeed (200 OK)
        res = self.client.get("/api/v1/documents", headers={"Authorization": f"Bearer {token1}"})
        self.assertEqual(res.status_code, 200)

        # 7. Revoke Token 1 (Token 2 remains active, so auth requirement is still active)
        res = self.client.delete(f"/api/v1/auth/tokens/{token1}", headers={"Authorization": f"Bearer {token2}"})
        self.assertEqual(res.status_code, 200)

        # 8. Accessing with the revoked Token 1 should now fail with 401
        res = self.client.get("/api/v1/documents", headers={"Authorization": f"Bearer {token1}"})
        self.assertEqual(res.status_code, 401)

        # 9. Accessing with the remaining Token 2 should succeed (200 OK)
        res = self.client.get("/api/v1/documents", headers={"Authorization": f"Bearer {token2}"})
        self.assertEqual(res.status_code, 200)

        # 10. Revoke the second token (Token 2)
        res = self.client.delete(f"/api/v1/auth/tokens/{token2}", headers={"Authorization": f"Bearer {token2}"})
        self.assertEqual(res.status_code, 200)

        # 11. Verify that auth is disabled again (no tokens exist)
        res = self.client.get("/api/v1/documents")
        self.assertEqual(res.status_code, 200)

    def test_search_endpoint(self):
        import src.db as db
        # 1. Search with no keys in db (bypasses auth due to first-run setup)
        res = self.client.post("/api/v1/search", json={"query": "test query", "limit": 3})
        self.assertEqual(res.status_code, 200)
        self.assertIn("No matches found", res.json()["results"])
        self.assertIn("synthesis", res.json())

        # 2. Add an agent token to activate auth
        token = "sk-pdfspecs-search-test-token"
        db.add_api_key(token, "Search Test Token")

        # 3. Call search without token -> should get 401 Unauthorized
        res = self.client.post("/api/v1/search", json={"query": "test query"})
        self.assertEqual(res.status_code, 401)

        # 4. Call search with token -> should succeed (200 OK)
        res = self.client.post("/api/v1/search", json={"query": "test query"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("No matches found", res.json()["results"])
        self.assertIn("synthesis", res.json())

        # 5. Call search with missing query payload -> should fail with 400
        res = self.client.post("/api/v1/search", json={}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 400)

    def test_metrics_endpoint(self):
        import src.db as db
        # 1. Get metrics without token (succeeds because auth is disabled if no keys exist)
        res = self.client.get("/api/v1/metrics")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("ingestion", data)
        self.assertIn("sandbox", data)
        self.assertIn("prompt", data["ingestion"])
        self.assertIn("completion", data["ingestion"])
        self.assertIn("total", data["ingestion"])

        # 2. Add an agent token to activate auth
        token = "sk-pdfspecs-metrics-test-token"
        db.add_api_key(token, "Metrics Test Token")

        # 3. Requesting without token should now fail with 401
        res = self.client.get("/api/v1/metrics")
        self.assertEqual(res.status_code, 401)

        # 4. Requesting with token should succeed
        res = self.client.get("/api/v1/metrics", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("ingestion", res.json())

if __name__ == "__main__":
    unittest.main()

