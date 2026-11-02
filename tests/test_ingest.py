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


if __name__ == "__main__":
    unittest.main()
