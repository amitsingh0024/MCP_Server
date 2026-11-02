"""
Search tests — the query-embedding gate and the `limit` clamp.

Both guard failures that quietly degraded search rather than breaking it:

* The semantic half was gated on an NVIDIA key even under the DEFAULT `local` embedding
  backend (BAAI/bge-m3), which runs offline and needs no key at all. Every local-first
  install — the entire reason the stdio MCP server exists — therefore ran lexical-only,
  with nothing in the logs to say so.
* `limit` arrives from a model (an MCP tool argument) or straight out of a request body,
  and multiplies into the candidate pool: `limit * 10` rows from BOTH the lexical and the
  vector index. The MCP tools capped it; the REST endpoints passed it to `int()` and on
  to Postgres, so one request could ask for millions of candidate rows.
"""
import os
import unittest
from unittest import mock

from src import search


class TestClampLimit(unittest.TestCase):
    def test_sane_values_pass_through(self):
        self.assertEqual(search.clamp_limit(3), 3)
        self.assertEqual(search.clamp_limit("7"), 7)

    def test_absurd_values_are_capped(self):
        self.assertEqual(search.clamp_limit(10 ** 9), search.MAX_SEARCH_LIMIT)

    def test_non_positive_becomes_one(self):
        self.assertEqual(search.clamp_limit(0), 1)
        self.assertEqual(search.clamp_limit(-5), 1)

    def test_junk_falls_back_to_the_default(self):
        """A non-numeric limit used to raise ValueError and surface as a 500."""
        for junk in (None, "abc", [], {}):
            self.assertEqual(search.clamp_limit(junk), search.DEFAULT_SEARCH_LIMIT,
                             repr(junk))


class TestQueryEmbeddingGate(unittest.TestCase):
    def _embed_query_with(self, backend: str, key: str):
        with mock.patch.dict(os.environ, {"PDFSPECS_EMBEDDING_BACKEND": backend}), \
             mock.patch.object(search.pgdb, "get_setting", return_value=key), \
             mock.patch.object(search.enricher, "embed_query",
                               return_value=[0.1, 0.2]) as embed:
            return search._embed_query("org-1", "treatment for fever"), embed

    def test_local_backend_embeds_without_an_api_key(self):
        """The regression that mattered: bge-m3 is offline, but the key check ran first."""
        result, embed = self._embed_query_with("local", "")
        self.assertIsNotNone(result)
        embed.assert_called_once()

    def test_nvidia_backend_still_requires_a_key(self):
        result, embed = self._embed_query_with("nvidia", "")
        self.assertIsNone(result)
        embed.assert_not_called()

    def test_nvidia_backend_embeds_when_a_key_is_configured(self):
        result, embed = self._embed_query_with("nvidia", "nvapi-test")
        self.assertIsNotNone(result)
        embed.assert_called_once()


class TestHybridSearchClamps(unittest.TestCase):
    def test_hybrid_search_clamps_its_own_limit(self):
        """
        The clamp lives in hybrid_search, not in each transport, so no caller can forget
        it — the REST endpoints did.
        """
        with mock.patch.object(search.pgdb, "search_lexical", return_value=[]) as lexical, \
             mock.patch.object(search, "_query_model_matches_corpus", return_value=False):
            search.hybrid_search("org-1", "jwara", limit=10 ** 6)
        self.assertEqual(lexical.call_args.kwargs["limit"],
                         max(50, search.MAX_SEARCH_LIMIT * 10))


if __name__ == "__main__":
    unittest.main()
