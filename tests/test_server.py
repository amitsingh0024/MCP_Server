"""
API surface tests — the local-mode auth bypass and document deletion.

* PDFSPECS_LOCAL_MODE makes require_admin skip JWT verification entirely and serve the
  implicit local org. auth.py asserted the DATABASE_URL was local but nothing checked the
  BIND ADDRESS — and nothing in the app can: uvicorn takes --host on the command line, and
  the Dockerfile passes 0.0.0.0. So `PDFSPECS_LOCAL_MODE=1` on a hosted box exposed every
  admin endpoint, POST /api/v1/keys included, to anyone. The check has to be per-request.
* DELETE /api/v1/documents/{id} ignored its result: an unknown id reported success, and a
  malformed one reached Postgres as a uuid cast error and came back as a 500.
"""
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src import server, auth

LOCAL_ADMIN = {"auth_uid": auth.LOCAL_OWNER_AUTH_ID, "email": auth.LOCAL_EMAIL,
               "org_id": "11111111-1111-1111-1111-111111111111"}


def _client(peer: str) -> TestClient:
    """
    A client whose requests appear to arrive from `peer`.

    Used WITHOUT `with`, so the app lifespan never runs: it would start the ingestion
    worker against a real database, and the MCP session manager it also enters refuses to
    be run twice in one process. These tests exercise route handlers, not startup.
    """
    return TestClient(server.app, client=(peer, 54321))


class TestLocalModeAdminGuard(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"PDFSPECS_LOCAL_MODE": "1",
                                           "PDFSPECS_ALLOW_REMOTE_LOCAL_MODE": ""})
        env.start()
        self.addCleanup(env.stop)
        admin = mock.patch.object(auth, "authenticate_admin", return_value=LOCAL_ADMIN)
        admin.start()
        self.addCleanup(admin.stop)

    def test_loopback_caller_is_served(self):
        r = _client("127.0.0.1").get("/api/v1/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["org_id"], LOCAL_ADMIN["org_id"])

    def test_remote_caller_is_refused(self):
        """The regression that mattered: no token, no Supabase, full admin access."""
        r = _client("203.0.113.7").get("/api/v1/me")
        self.assertEqual(r.status_code, 403)
        # The message must name the way out, or an operator just unsets the wrong thing.
        self.assertIn("PDFSPECS_ALLOW_REMOTE_LOCAL_MODE", r.json()["detail"])

    def test_key_minting_is_refused_too(self):
        """POST /api/v1/keys mints a real org API key; it must be behind the same guard."""
        r = _client("203.0.113.7").post("/api/v1/keys", json={"description": "x"})
        self.assertEqual(r.status_code, 403)

    def test_remote_caller_is_served_when_explicitly_overridden(self):
        with mock.patch.dict(os.environ, {"PDFSPECS_ALLOW_REMOTE_LOCAL_MODE": "1"}):
            r = _client("203.0.113.7").get("/api/v1/me")
        self.assertEqual(r.status_code, 200)

    def test_guard_does_not_apply_outside_local_mode(self):
        """Without the bypass, normal JWT verification governs access from anywhere."""
        with mock.patch.dict(os.environ, {"PDFSPECS_LOCAL_MODE": ""}):
            r = _client("203.0.113.7").get("/api/v1/me")
        self.assertEqual(r.status_code, 200)


class TestDeleteDocument(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"PDFSPECS_LOCAL_MODE": "1"})
        env.start()
        self.addCleanup(env.stop)
        admin = mock.patch.object(auth, "authenticate_admin", return_value=LOCAL_ADMIN)
        admin.start()
        self.addCleanup(admin.stop)

    def test_deleting_an_existing_document_succeeds(self):
        with mock.patch.object(server.pgdb, "delete_document", return_value=True):
            r = _client("127.0.0.1").delete("/api/v1/documents/3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        self.assertEqual(r.status_code, 200)

    def test_unknown_id_is_404_not_a_false_success(self):
        with mock.patch.object(server.pgdb, "delete_document", return_value=False):
            r = _client("127.0.0.1").delete("/api/v1/documents/3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        self.assertEqual(r.status_code, 404)

    def test_malformed_id_is_404_not_a_500(self):
        """documents.id is a uuid column; the junk never reaches the cast."""
        r = _client("127.0.0.1").delete("/api/v1/documents/not-a-uuid")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
