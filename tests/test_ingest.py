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

from src import ingest, enricher, pgdb


class _Stop(BaseException):
    """
    Breaks run_worker_loop's `while True` at a known point.

    BaseException, not Exception, and that is the whole point: the loop now catches
    Exception on every iteration so a dropped connection cannot kill the worker thread.
    A test sentinel deriving from Exception would be swallowed by that handler and spin
    forever. Deriving from BaseException keeps it outside the contract under test.
    """


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


class TestSourceFormatDispatch(unittest.TestCase):
    """
    These books exist as both PDF and markdown, and the markdown is the source the PDFs
    were printed from. Sending a .md file down the PDF path would hand pymupdf a file it
    cannot open; sending a .pdf down the markdown path would index its raw bytes. The
    extension is what picks, so pin it.
    """

    def _run(self, stored_name):
        calls = {"pdf": 0, "md": 0}

        def parse_pdf(path, progress_callback=None):
            calls["pdf"] += 1
            return {"sha256": "abc", "size_bytes": 1, "pages": []}

        def parse_markdown(path, progress_callback=None, page_map=None):
            calls["md"] += 1
            return {"sha256": "abc", "size_bytes": 1, "pages": []}

        with mock.patch.object(ingest, "_org_provider_settings") as settings, \
             mock.patch.object(ingest.storage, "safe_filename", return_value=stored_name), \
             mock.patch.object(ingest.storage, "download_to_path"), \
             mock.patch.object(ingest.parser, "parse_pdf", side_effect=parse_pdf), \
             mock.patch.object(ingest.parser, "parse_markdown", side_effect=parse_markdown), \
             mock.patch.object(ingest.chunker, "chunk_document", return_value=[]), \
             mock.patch.object(ingest.pgdb, "get_document_by_sha256", return_value=None), \
             mock.patch.object(ingest.pgdb, "add_document", return_value="doc-1"), \
             mock.patch.object(ingest.pgdb, "update_task"), \
             mock.patch.object(ingest.pgdb, "update_task_progress"), \
             mock.patch.object(ingest.pgdb, "add_chunks_bulk", return_value=[]), \
             mock.patch.object(ingest, "_embed_chunks", return_value=([], 0)):
            settings.return_value.resolved_embedding_model.return_value = "some/model"
            ingest.process_task({"id": 1, "org_id": "org-1", "storage_key": "k",
                                 "original_filename": stored_name, "attempts": 0})
        return calls

    def test_pdf_goes_to_the_pdf_parser(self):
        self.assertEqual(self._run("book.pdf"), {"pdf": 1, "md": 0})

    def test_markdown_goes_to_the_markdown_parser(self):
        self.assertEqual(self._run("book.md"), {"pdf": 0, "md": 1})

    def test_markdown_long_extension_is_recognised(self):
        self.assertEqual(self._run("book.markdown"), {"pdf": 0, "md": 1})

    def test_extension_matching_ignores_case(self):
        self.assertEqual(self._run("BOOK.MD"), {"pdf": 0, "md": 1})


class TestWorkerLoopSurvivesErrors(unittest.TestCase):
    """
    The worker is a daemon thread inside the API process. When an exception escaped the
    loop the thread ended silently: uvicorn kept serving, /health kept returning ok, and
    the queue simply stopped draining until someone restarted the container. Observed in
    production with 13 tasks pending and a warm instance answering /health in 0.13s.
    """

    def test_a_transient_poll_failure_does_not_kill_the_loop(self):
        # First poll blows up the way a dropped pooled connection does; the loop must
        # take the next pass rather than end.
        polls = [ConnectionError("server closed the connection unexpectedly"), _Stop()]
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0), \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=polls) as poll, \
             mock.patch("time.sleep"):
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0)
        self.assertEqual(poll.call_count, 2,
                         "the loop must poll again after a transient failure")

    def test_a_failure_inside_process_task_does_not_kill_the_loop(self):
        task = {"id": 1, "org_id": "o", "storage_key": "k", "original_filename": "f.md"}
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0), \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=[task, _Stop()]), \
             mock.patch.object(ingest, "process_task",
                               side_effect=RuntimeError("boom")) as proc, \
             mock.patch("time.sleep"):
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0)
        self.assertEqual(proc.call_count, 1)

    def test_the_loop_records_a_heartbeat(self):
        ingest._worker_state["iterations"] = 0
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0), \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=[None, _Stop()]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_embedding", return_value=[]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_enrichment", return_value=[]), \
             mock.patch("time.sleep"):
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0)
        state = ingest.worker_state()
        self.assertIsNotNone(state["started_at"])
        self.assertIsNotNone(state["heartbeat"])
        self.assertGreaterEqual(state["iterations"], 1)


class TestStuckTaskSweep(unittest.TestCase):
    def test_stale_tasks_are_reswept_during_the_loop_not_only_at_startup(self):
        """
        A worker that dies mid-book leaves its task 'processing'. Nothing reclaimed it
        while another worker was already running, because the sweep ran once at startup
        — one task sat stranded for an hour against a 15-minute lease and only a
        container restart freed it.
        """
        clock = iter([0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=2) as sweep, \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=[None, _Stop()]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_embedding", return_value=[]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_enrichment", return_value=[]), \
             mock.patch("time.monotonic", side_effect=lambda: next(clock, 10_000.0)), \
             mock.patch("time.sleep"):
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0)
        self.assertGreaterEqual(sweep.call_count, 2,
                                "expected the startup sweep plus at least one in-loop sweep")

    def test_the_in_loop_sweep_is_more_cautious_than_the_startup_one(self):
        """
        Reclaiming a task deletes its partial document, so a sweep that fires every few
        minutes must not steal from a worker that is merely slow. A 32-chunk embedding
        batch has been measured at ~10 minutes on a starved instance, against a
        15-minute lease.
        """
        clock = iter([0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
        with mock.patch.object(ingest.pgdb, "reset_stuck_tasks", return_value=0) as sweep, \
             mock.patch.object(ingest.pgdb, "get_next_task", side_effect=[None, _Stop()]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_embedding", return_value=[]), \
             mock.patch.object(ingest.pgdb, "orgs_with_pending_enrichment", return_value=[]), \
             mock.patch("time.monotonic", side_effect=lambda: next(clock, 10_000.0)), \
             mock.patch("time.sleep"):
            with self.assertRaises(_Stop):
                ingest.run_worker_loop(interval_sec=0)

        startup, in_loop = sweep.call_args_list[0], sweep.call_args_list[1]
        self.assertIsNone(startup.kwargs.get("lease_minutes"),
                          "startup should use the ordinary lease; nothing of ours is in flight")
        self.assertGreater(in_loop.kwargs["lease_minutes"], pgdb.TASK_LEASE_MINUTES)


class TestEmptyDocumentIsNotTreatedAsIngested(unittest.TestCase):
    """
    The document row is created before chunking, so an ingest killed in between leaves
    one holding zero chunks. The sha256 diff-skip keyed on the row existing, so every
    later upload of that file reported "already ingested" over an empty document —
    silently, and permanently.
    """

    def _process(self, existing, chunk_count):
        calls = {}

        def add_document(org, filename, sha, size):
            calls["created"] = True
            return "doc-new"

        with mock.patch.object(ingest, "_org_provider_settings") as settings, \
             mock.patch.object(ingest.storage, "safe_filename", return_value="f.md"), \
             mock.patch.object(ingest.storage, "download_to_path"), \
             mock.patch.object(ingest.parser, "parse_markdown",
                               return_value={"sha256": "abc", "size_bytes": 1, "pages": []}), \
             mock.patch.object(ingest.chunker, "chunk_document", return_value=[]), \
             mock.patch.object(ingest.pgdb, "get_document_by_sha256", return_value=existing), \
             mock.patch.object(ingest.pgdb, "document_chunk_count", return_value=chunk_count), \
             mock.patch.object(ingest.pgdb, "delete_document", return_value=True) as delete, \
             mock.patch.object(ingest.pgdb, "add_document", side_effect=add_document), \
             mock.patch.object(ingest.pgdb, "update_task"), \
             mock.patch.object(ingest.pgdb, "update_task_progress"), \
             mock.patch.object(ingest.pgdb, "add_chunks_bulk", return_value=[]), \
             mock.patch.object(ingest, "_embed_chunks", return_value=([], 0)):
            settings.return_value.resolved_embedding_model.return_value = "m"
            ingest.process_task({"id": 1, "org_id": "org-1", "storage_key": "k",
                                 "original_filename": "f.md", "attempts": 0})
        return calls, delete

    def test_a_document_with_chunks_is_skipped(self):
        calls, delete = self._process({"id": "doc-old"}, 42)
        self.assertNotIn("created", calls, "a finished document must not be re-ingested")
        delete.assert_not_called()

    def test_an_empty_document_is_deleted_and_reingested(self):
        calls, delete = self._process({"id": "doc-old"}, 0)
        delete.assert_called_once()
        self.assertTrue(calls.get("created"),
                        "an abandoned empty document must be ingested again, not skipped")
