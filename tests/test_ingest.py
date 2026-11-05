"""
Ingestion worker tests — enrichment fairness and crash-recovery scoping.

Both cover multi-tenant / multi-worker failures that a single-org, single-worker setup
can never show:

* One org without an NVIDIA key aborted the ENTIRE enrichment scan, because the
  APIKeyMissingError handler sat outside the per-org loop. orgs_with_pending_enrichment()
  is a DISTINCT with no ORDER BY, so whether anyone got enriched came down to row order.
* reset_stuck_tasks() requeued every task in 'processing' across all orgs and deleted each
  one's partial document. Correct for one worker (anything left over belongs to the
  process that just died); with two, each startup destroyed the document the other was
  actively writing.
"""
import os
import unittest
from unittest import mock

from src import ingest, enricher


class _Stop(Exception):
    """Breaks run_worker_loop's `while True` at a known point."""


class TestEnrichmentFairness(unittest.TestCase):
    def _run_loop(self, orgs, enrich_pass):
        """Drive one idle iteration of the worker loop, then stop it."""
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0), \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=[None, _Stop()]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_enrichment",
                               return_value=orgs), \
             mock.patch.object(ingest, "run_enrichment_pass", side_effect=enrich_pass), \
             mock.patch("time.sleep") as sleep:
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0)
            return sleep

    def test_a_keyless_org_does_not_starve_the_others(self):
        """The regression that mattered: org order decided whether anyone got enriched."""
        seen = []

        def enrich_pass(org, limit=None):
            seen.append(org)
            if org == "org-no-key":
                raise enricher.APIKeyMissingError("no key configured")
            return 5

        self._run_loop(["org-no-key", "org-with-key"], enrich_pass)
        self.assertEqual(seen, ["org-no-key", "org-with-key"])

    def test_a_failing_org_does_not_abort_the_scan(self):
        """A provider error for one tenant is not a reason to skip every other tenant."""
        seen = []

        def enrich_pass(org, limit=None):
            seen.append(org)
            if org == "org-broken":
                raise RuntimeError("provider exploded")
            return 3

        self._run_loop(["org-broken", "org-healthy"], enrich_pass)
        self.assertEqual(seen, ["org-broken", "org-healthy"])

    def test_nothing_enrichable_sleeps_instead_of_spinning(self):
        def enrich_pass(org, limit=None):
            raise enricher.APIKeyMissingError("no key configured")

        sleep = self._run_loop(["org-a", "org-b"], enrich_pass)
        sleep.assert_called_once()


class TestCrashRecoveryScoping(unittest.TestCase):
    def test_recovery_and_claims_both_carry_this_worker_identity(self):
        """
        reset_stuck_tasks must be told who is asking. Swept with no identity it reclaimed
        every 'processing' task and deleted its document — including another live worker's.
        """
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0) as reset, \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=_Stop) as claim:
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(enrich=False)
            me = ingest.worker_id()
            reset.assert_called_once_with(worker_id=me)
            claim.assert_called_once_with(worker_id=me)

    def test_worker_id_is_stable_within_a_process_and_names_the_pid(self):
        """A restarted process must get a new identity, or it reclaims a sibling's work."""
        self.assertEqual(ingest.worker_id(), ingest.worker_id())
        self.assertTrue(ingest.worker_id().endswith(f":{os.getpid()}"))


class TestEmbeddingFailureDoesNotDiscardTheParse(unittest.TestCase):
    """
    An embedding outage must cost vectors, not the whole document.

    The regression that mattered: embedding runs AFTER all parsing, and a raise there
    aborted the task and threw the parse away. On this corpus that is up to nine hours of
    OCR discarded because of one transient 5xx — which is exactly what happened on
    2026-11-05 (`500 service inference failed` from /v1/embeddings after 65 minutes).
    """

    def test_failed_batch_yields_empty_vectors_and_is_counted(self):
        chunks = [{"text_content": f"chunk {i}"} for i in range(3)]
        with mock.patch.object(ingest, "_embed_chunk_batch", return_value=10), \
             mock.patch.object(ingest.pgdb, "update_task_progress"), \
             mock.patch.object(ingest.enricher, "embed_texts_batch",
                               side_effect=RuntimeError("500 service inference failed")):
            vectors, unembedded = ingest._embed_chunks(chunks, mock.Mock(), 1)
        self.assertEqual(vectors, [[], [], []])
        self.assertEqual(unembedded, 3)

    def test_successful_batches_keep_their_vectors(self):
        """Partial credit: only the batch that failed loses its vectors."""
        chunks = [{"text_content": f"chunk {i}"} for i in range(4)]
        calls = {"n": 0}

        def embed(batch, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return [[[0.1]] for _ in batch]

        with mock.patch.object(ingest, "_embed_chunk_batch", return_value=2), \
             mock.patch.object(ingest.pgdb, "update_task_progress"), \
             mock.patch.object(ingest.enricher, "embed_texts_batch", side_effect=embed):
            vectors, unembedded = ingest._embed_chunks(chunks, mock.Mock(), 1)
        self.assertEqual(vectors[:2], [[], []])
        self.assertEqual(vectors[2:], [[[0.1]], [[0.1]]])
        self.assertEqual(unembedded, 2)

    def test_unembedded_chunks_are_stored_without_claiming_a_model(self):
        """
        embedding_model is what corpus_embedding_models() reports, and search compares it
        against the live query model. Recording a model for chunks that hold no vectors
        would make that check pass on a corpus it knows nothing about.
        """
        captured = {}

        def add_chunks_bulk(org, doc, rows):
            captured["rows"] = rows
            return [f"id{i}" for i in range(len(rows))]

        chunks = [{"page_start": 1, "page_end": 1, "text_content": "a"},
                  {"page_start": 2, "page_end": 2, "text_content": "b"}]
        with mock.patch.object(ingest, "_org_provider_settings") as settings, \
             mock.patch.object(ingest.storage, "safe_filename", return_value="f.pdf"), \
             mock.patch.object(ingest.storage, "download_to_path"), \
             mock.patch.object(ingest.parser, "parse_pdf",
                               return_value={"sha256": "abc", "size_bytes": 1, "pages": []}), \
             mock.patch.object(ingest.chunker, "chunk_document", return_value=chunks), \
             mock.patch.object(ingest.pgdb, "get_document_by_sha256", return_value=None), \
             mock.patch.object(ingest.pgdb, "add_document", return_value="doc-1"), \
             mock.patch.object(ingest.pgdb, "update_task"), \
             mock.patch.object(ingest.pgdb, "update_task_progress"), \
             mock.patch.object(ingest.pgdb, "add_chunks_bulk", side_effect=add_chunks_bulk), \
             mock.patch.object(ingest, "_embed_chunks", return_value=([[], [[0.2]]], 1)):
            settings.return_value.resolved_embedding_model.return_value = "some/model"
            ok = ingest.process_task({"id": 1, "org_id": "org-1", "storage_key": "k",
                                      "original_filename": "f.pdf", "attempts": 0})

        self.assertTrue(ok, "an embedding outage must not fail the task")
        rows = captured["rows"]
        self.assertIsNone(rows[0]["embedding_model"], "no vectors -> no model claimed")
        self.assertEqual(rows[1]["embedding_model"], "some/model")
        self.assertEqual(rows[0]["text_content"], "a", "the OCR'd text is still stored")


class TestEmbeddingBackfill(unittest.TestCase):
    def test_backfill_commits_vectors_and_records_the_model(self):
        batches = [[{"id": "c1", "text_content": "x"}], []]
        saved = []
        with mock.patch.object(ingest, "_org_provider_settings") as settings, \
             mock.patch.object(ingest.pgdb, "chunks_pending_embedding",
                               side_effect=batches), \
             mock.patch.object(ingest.pgdb, "record_token_usage"), \
             mock.patch.object(ingest.pgdb, "save_chunk_embeddings",
                               side_effect=lambda *a: saved.append(a)), \
             mock.patch.object(ingest.enricher, "embed_texts_batch",
                               return_value=[[[0.3]]]):
            settings.return_value.resolved_embedding_model.return_value = "some/model"
            done = ingest.run_embedding_pass("org-1")
        self.assertEqual(done, 1)
        self.assertEqual(saved[0][1], "c1")
        self.assertEqual(saved[0][3], "some/model")

    def test_unembeddable_chunk_does_not_loop_forever(self):
        """
        The query returns oldest-first, so a chunk that yields no windows (blank text)
        would be re-fetched for ever. The pass must notice and stop.
        """
        stuck = [{"id": "blank", "text_content": "   "}]
        with mock.patch.object(ingest, "_org_provider_settings") as settings, \
             mock.patch.object(ingest.pgdb, "chunks_pending_embedding",
                               return_value=stuck), \
             mock.patch.object(ingest.pgdb, "record_token_usage"), \
             mock.patch.object(ingest.pgdb, "save_chunk_embeddings") as save, \
             mock.patch.object(ingest.enricher, "embed_texts_batch", return_value=[[]]):
            settings.return_value.resolved_embedding_model.return_value = "m"
            done = ingest.run_embedding_pass("org-1")
        self.assertEqual(done, 0)
        save.assert_not_called()

    def test_worker_backfills_embeddings_even_with_enrichment_disabled(self):
        """`enrich` governs the LLM stage only; vectors need no LLM."""
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0), \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=[None, _Stop()]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_embedding",
                               return_value=["org-1"]), \
             mock.patch.object(ingest, "run_embedding_pass", return_value=5) as backfill, \
             mock.patch.object(ingest, "run_enrichment_pass") as enrich_pass, \
             mock.patch("time.sleep"):
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0, enrich=False)
        backfill.assert_called_once()
        enrich_pass.assert_not_called()


if __name__ == "__main__":
    unittest.main()
