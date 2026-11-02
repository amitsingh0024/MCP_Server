"""
Enricher tests — embedding window sizing and batched enrichment alignment.

Both cover bugs that reached a live corpus load:

* A fixed 900-character embedding window is ~225 tokens of English but 576 tokens of
  Devanagari, over nv-embedqa-e5-v5's 512 cap. The API returns 400 (a client error, so
  not retried) and two whole documents failed. Nothing tested a non-Latin script.
* Batching several chunks into one LLM call risks the model dropping or reordering
  results, which would attach summaries to the wrong chunks — worse than having none.
"""
import os
import json
import unittest
from unittest import mock

from requests.exceptions import HTTPError

from src import enricher


DEVANAGARI = "अथातो ज्वरचिकित्सितं व्याख्यास्यामः। "
ENGLISH = "Now we shall discourse on the treatment of fever and its remedies. "


class TestEmbeddingWindows(unittest.TestCase):
    def _est_tokens(self, s: str) -> float:
        """Same heuristic the sizer uses, applied independently as a check."""
        dev = len(enricher._DEVANAGARI_RE.findall(s))
        ratio = dev / len(s) if s else 0
        return len(s) * (enricher._TOK_PER_CHAR_DEVANAGARI * ratio
                         + enricher._TOK_PER_CHAR_OTHER * (1 - ratio))

    def test_devanagari_windows_stay_under_the_token_cap(self):
        text = DEVANAGARI * 40
        for w in enricher._windows(text):
            self.assertLessEqual(
                self._est_tokens(w), 512,
                "a window must never exceed nv-embedqa-e5-v5's 512-token limit")

    def test_devanagari_gets_smaller_windows_than_english(self):
        """The whole point: character count alone can't size a window across scripts."""
        self.assertLess(enricher._window_chars_for(DEVANAGARI * 40),
                        enricher._window_chars_for(ENGLISH * 40))

    def test_mixed_script_sizes_between_the_two(self):
        mixed = (DEVANAGARI * 20) + (ENGLISH * 20)
        self.assertLess(enricher._window_chars_for(DEVANAGARI * 40),
                        enricher._window_chars_for(mixed))
        self.assertLess(enricher._window_chars_for(mixed),
                        enricher._window_chars_for(ENGLISH * 40))

    def test_empty_and_whitespace_produce_no_windows(self):
        """The API rejects empty elements with 400, failing the entire batch."""
        for value in ("", "   ", "\n\t "):
            self.assertEqual(enricher._windows(value), [])

    def test_windows_cover_the_whole_text(self):
        """
        No character may be dropped. Built from distinct Devanagari words rather than one
        repeated phrase: periodic text makes overlap detection ambiguous and would let a
        genuinely lossy windowing pass.
        """
        words = ["ज्वरः", "कासः", "श्वासः", "अतिसारः", "पाण्डुः", "शोफः", "गुल्मः",
                 "अर्शांसि", "प्रमेहः", "कुष्ठम्", "वातम्", "पित्तम्", "कफः", "रक्तम्"]
        # Index each word so every position in the text is unique.
        text = " ".join(f"{w}{i}" for i in range(120) for w in words)
        windows = enricher._windows(text)
        self.assertGreater(len(windows), 1)

        # Walk the windows through the source; each must appear at or after the cursor,
        # and consecutive windows must overlap or abut (never skip).
        cursor = 0
        end_reached = 0
        for w in windows:
            pos = text.find(w, max(0, cursor - len(w)))
            self.assertNotEqual(pos, -1, "window is not a substring of the source")
            self.assertLessEqual(pos, end_reached, "gap between windows — content dropped")
            end_reached = max(end_reached, pos + len(w))
            cursor = pos
        self.assertEqual(end_reached, len(text), "windows do not reach the end of the text")


def _llm_response(payload: dict) -> mock.Mock:
    r = mock.Mock()
    r.json.return_value = {"choices": [{"message": {"content": json.dumps(payload)}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return r


class TestBatchEnrichment(unittest.TestCase):
    def setUp(self):
        self.settings = enricher.ProviderSettings(nvidia_api_key="nvapi-test")
        self.texts = ["chunk one text", "chunk two text", "chunk three text"]

    def test_results_are_realigned_by_echoed_id(self):
        """Out-of-order results must be reordered by id, not trusted positionally."""
        payload = {"results": [
            {"id": 3, "summary": "third", "entities": []},
            {"id": 1, "summary": "first", "entities": []},
            {"id": 2, "summary": "second", "entities": []},
        ]}
        with mock.patch.object(enricher, "_post_with_retry", return_value=_llm_response(payload)):
            out = enricher.extract_metadata_batch(self.texts, self.settings)
        self.assertEqual([m["summary"] for m in out], ["first", "second", "third"])

    def test_missing_id_raises_so_caller_can_fall_back(self):
        """A dropped chunk must fail loudly — silently shifting summaries is far worse."""
        payload = {"results": [
            {"id": 1, "summary": "first", "entities": []},
            {"id": 3, "summary": "third", "entities": []},
        ]}
        with mock.patch.object(enricher, "_post_with_retry", return_value=_llm_response(payload)):
            with self.assertRaises(ValueError):
                enricher.extract_metadata_batch(self.texts, self.settings)

    def test_wrong_count_without_ids_raises(self):
        payload = {"results": [{"summary": "only one", "entities": []}]}
        with mock.patch.object(enricher, "_post_with_retry", return_value=_llm_response(payload)):
            with self.assertRaises(ValueError):
                enricher.extract_metadata_batch(self.texts, self.settings)

    def test_positional_fallback_when_no_ids_but_count_matches(self):
        payload = {"results": [{"summary": s, "entities": []} for s in ("a", "b", "c")]}
        with mock.patch.object(enricher, "_post_with_retry", return_value=_llm_response(payload)):
            out = enricher.extract_metadata_batch(self.texts, self.settings)
        self.assertEqual([m["summary"] for m in out], ["a", "b", "c"])

    def test_malformed_entities_are_sanitized(self):
        payload = {"results": [
            {"id": 1, "summary": "s", "entities": [
                {"name": "Jwara", "type": "bogus-type"},   # unknown type -> concept
                {"name": "", "type": "concept"},           # empty name -> dropped
                "not-a-dict",                              # junk -> dropped
                {"name": "  Guduchi  ", "type": "PERSON"}, # trimmed, lowercased
            ]},
            {"id": 2, "summary": 42, "entities": None},    # wrong types throughout
            {"id": 3, "summary": "s3", "entities": []},
        ]}
        with mock.patch.object(enricher, "_post_with_retry", return_value=_llm_response(payload)):
            out = enricher.extract_metadata_batch(self.texts, self.settings)
        self.assertEqual([e["name"] for e in out[0]["entities"]], ["Jwara", "Guduchi"])
        self.assertEqual(out[0]["entities"][0]["type"], "concept")
        self.assertEqual(out[0]["entities"][1]["type"], "person")
        self.assertEqual(out[1]["summary"], "42")
        self.assertEqual(out[1]["entities"], [])

    def test_single_text_uses_the_unbatched_path(self):
        with mock.patch.object(enricher, "extract_metadata",
                               return_value={"summary": "solo", "entities": []}) as single:
            out = enricher.extract_metadata_batch(["only one"], self.settings)
        single.assert_called_once()
        self.assertEqual(out[0]["summary"], "solo")

    def test_missing_api_key_raises(self):
        with self.assertRaises(enricher.APIKeyMissingError):
            enricher.extract_metadata_batch(self.texts, enricher.ProviderSettings())


class TestEmbeddingBatchAlignment(unittest.TestCase):
    """
    Window vectors must stay attached to the chunk they were produced from.

    The nvidia path halves a window and retries when the API rejects it for exceeding the
    512-token cap, so one window can legitimately yield several vectors. While the split
    returned those FLAT, embed_texts_batch — which zips vectors against window owners
    positionally — shifted every vector after the split onto the wrong chunk and dropped
    the tail, so chunks ended up semantically indexed with a neighbour's text. Nothing
    reported it: ingestion succeeded and search just returned the wrong passages.
    """
    DIM = 4

    def setUp(self):
        self.settings = enricher.ProviderSettings(nvidia_api_key="nvapi-test")
        env = mock.patch.dict(os.environ, {"PDFSPECS_EMBEDDING_BACKEND": "nvidia"})
        env.start()
        self.addCleanup(env.stop)

    def _tagged(self, batch) -> mock.Mock:
        """One vector per input window, tagged with that window's first character."""
        r = mock.Mock()
        r.json.return_value = {
            "data": [{"index": i, "embedding": [float(ord(w[0]))] * self.DIM}
                     for i, w in enumerate(batch)],
            "usage": {"prompt_tokens": 1},
        }
        return r

    @staticmethod
    def _token_limit_error() -> HTTPError:
        resp = mock.Mock()
        resp.status_code = 400
        resp.text = "Input length 576 exceeds maximum allowed token size 512"
        return HTTPError("400 Client Error :: Input length 576 exceeds maximum allowed "
                         "token size 512", response=resp)

    @staticmethod
    def _sources(out) -> list:
        """For each input text, which texts its vectors actually came from."""
        return [sorted({chr(int(v[0])) for v in vecs}) for vecs in out]

    def test_vectors_follow_their_own_chunk_through_a_split(self):
        """The regression that mattered: 'B' and 'C' were indexed with 'A' content."""
        calls = {"n": 0}

        def post(url, json_payload=None, headers=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:                     # the first request hits the token cap
                raise self._token_limit_error()
            return self._tagged(json_payload["input"])

        texts = ["A" * 3000, "B" * 50, "C" * 50]
        windows = [len(enricher._windows(t)) for t in texts]
        self.assertGreater(windows[0], 1, "fixture must produce a multi-window chunk")

        with mock.patch.object(enricher, "_post_with_retry", side_effect=post):
            out = enricher.embed_texts_batch(texts, settings=self.settings)

        self.assertEqual(self._sources(out), [["A"], ["B"], ["C"]])
        # Both halves of every split window are kept, under the chunk that owned them.
        self.assertEqual(len(out[0]), windows[0] * 2)
        self.assertEqual([len(out[1]), len(out[2])], [1, 1])

    def test_unsplit_batches_keep_one_vector_per_window(self):
        texts = ["A" * 3000, "B" * 50]
        with mock.patch.object(
                enricher, "_post_with_retry",
                side_effect=lambda url, json_payload=None, **kw: self._tagged(
                    json_payload["input"])):
            out = enricher.embed_texts_batch(texts, settings=self.settings)
        self.assertEqual(self._sources(out), [["A"], ["B"]])
        self.assertEqual([len(v) for v in out],
                         [len(enricher._windows(t)) for t in texts])

    def test_vector_count_mismatch_raises_instead_of_misattributing(self):
        """A backend that returns the wrong count must fail loudly, never guess a mapping."""
        with mock.patch.object(
                enricher, "_post_with_retry",
                side_effect=lambda url, json_payload=None, **kw: self._tagged(
                    json_payload["input"][:-1])):
            with self.assertRaises(ValueError):
                enricher.embed_texts_batch(["A" * 3000, "B" * 50], settings=self.settings)


if __name__ == "__main__":
    unittest.main()
