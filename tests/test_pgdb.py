"""
pgdb tests for the embedding dimension guard (REVAMP B-1) and the uuid id guard.

Only the pure, connection-free logic is covered here — the two gates every value passes
through before it reaches Postgres:

* `_clean_embedding`, whose old silent-drop behaviour let an entire corpus ingest
  "successfully" with zero searchable embeddings;
* `is_valid_uuid`, without which a model-invented chunk id became an unhandled
  `invalid input syntax for type uuid` instead of "not found".
"""
import uuid
import unittest
from unittest import mock

from src import pgdb


class TestCleanEmbedding(unittest.TestCase):
    def test_correct_dimension_passes_through(self):
        vec = [0.1] * pgdb.EMBEDDING_DIM
        self.assertEqual(pgdb._clean_embedding(vec), vec)

    def test_absent_embedding_is_none_not_an_error(self):
        """A missing vector is a legitimate degraded state: lexical-only for that chunk."""
        self.assertIsNone(pgdb._clean_embedding(None))
        self.assertIsNone(pgdb._clean_embedding([]))

    def test_wrong_dimension_raises_instead_of_silently_dropping(self):
        """
        The regression that mattered: a 768-d Gemini vector against a vector(1024) column
        was dropped with a log warning, ingestion reported success, and semantic search
        returned nothing forever after.
        """
        with self.assertRaises(pgdb.EmbeddingDimError) as ctx:
            pgdb._clean_embedding([0.1] * 768)
        msg = str(ctx.exception)
        # The message must name both dimensions — that's what makes it actionable.
        self.assertIn("768", msg)
        self.assertIn(str(pgdb.EMBEDDING_DIM), msg)

    def test_error_is_a_valueerror_subclass(self):
        """Callers catching ValueError keep working; the task still fails loudly."""
        self.assertTrue(issubclass(pgdb.EmbeddingDimError, ValueError))


class TestUuidGuard(unittest.TestCase):
    """
    documents.id and chunks.id are uuid columns, and ids arrive from places we don't
    control: an MCP tool argument (straight from a model, which will occasionally invent
    one) or a URL path. A non-UUID string reached Postgres as `invalid input syntax for
    type uuid` — an unhandled exception out of the MCP tool and a 500 from the API, where
    a plain "not found" was both correct and already implemented one line below.
    """

    def test_well_formed_uuids_are_accepted(self):
        text = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        self.assertTrue(pgdb.is_valid_uuid(text))
        self.assertTrue(pgdb.is_valid_uuid(uuid.UUID(text)))

    def test_invented_ids_are_rejected(self):
        for bad in ("chunk-1", "", None, 42, "not-a-uuid",
                    "3f2504e0-4f89-11d3-9a0c"):          # truncated
            self.assertFalse(pgdb.is_valid_uuid(bad), repr(bad))

    def test_a_malformed_id_never_reaches_the_database(self):
        """A miss is decided in Python; Postgres is never asked to cast the junk."""
        with mock.patch.object(pgdb, "connection") as conn:
            self.assertIsNone(pgdb.get_chunk("org-1", "chunk-1"))
            self.assertEqual(pgdb.get_chunks_by_ids("org-1", ["chunk-1", "nope"]), {})
            self.assertFalse(pgdb.delete_document("org-1", "chunk-1"))
            conn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
