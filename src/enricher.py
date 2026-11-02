"""
Chunk enrichment against NVIDIA NIM — summary/keywords/entities via an LLM, plus the
embedding vectors used for semantic search.

NVIDIA is the only provider (REVAMP Part A). One provider means one embedding dimension,
which is what the `vector(1024)` column has always assumed.

Embeddings are MULTI-VECTOR: a chunk longer than one model window is embedded as several
window vectors and stored as several rows, scored later by best-window (max-sim). The
previous mean-pooling both truncated past 8 windows and averaged away the signal of the
one window that actually matched (REVAMP B-2).
"""
import os
import re
import json
import logging
import time
import threading
from dataclasses import dataclass
import requests
from requests.exceptions import RequestException, HTTPError

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

logger = logging.getLogger("pdfspecs.enricher")


class APIKeyMissingError(ValueError):
    pass


# Global request throttle — spaces out ALL provider API calls (across worker threads) to
# respect free-tier rate limits (e.g. NVIDIA 429s). Ingestion gets slower but reliable.
# PDFSPECS_API_MIN_INTERVAL seconds between request starts; 0 disables. Default ~40 req/min.
_MIN_API_INTERVAL = float(os.getenv("PDFSPECS_API_MIN_INTERVAL", "1.5"))
_rate_lock = threading.Lock()
_last_request_ts = 0.0


def _throttle():
    """Block until at least _MIN_API_INTERVAL has elapsed since the last request start."""
    global _last_request_ts
    if _MIN_API_INTERVAL <= 0:
        return
    with _rate_lock:  # held across the sleep so all threads are spaced globally
        wait = _last_request_ts + _MIN_API_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()


NVIDIA_LLM_MODEL = "meta/llama-3.1-8b-instruct"

# Two embedding backends, BOTH 1024-d, so they share the vector(1024) column and switching
# needs no migration. Selected by PDFSPECS_EMBEDDING_BACKEND; `local` is the default.
#
#   local  — BAAI/bge-m3 via sentence-transformers. Fully offline, no quota, strong
#            multilingual coverage. Cost is throughput: measured on this host (Intel
#            i7-8750H, 2018) it runs ~19.8 chunks/min on CPU and ~8.3 on MPS — about
#            13 hours for a 15,700-chunk corpus.
#   nvidia — nv-embedqa-e5-v5 over the NIM API. Batches 32 windows per request, so the
#            same corpus takes ~30 minutes even at the 1.5s throttle. Costs API quota.
#
# NOTE for `local`: MPS is *slower* than CPU on an Intel Mac (the "GPU" is a weak
# integrated part, not Apple Silicon). Set PDFSPECS_EMBED_DEVICE=cpu there.
LOCAL_EMBEDDING_MODEL = "BAAI/bge-m3"                # 1024-d
NVIDIA_EMBEDDING_MODEL = "nvidia/nv-embedqa-e5-v5"   # 1024-d


def embedding_backend() -> str:
    b = os.getenv("PDFSPECS_EMBEDDING_BACKEND", "local").strip().lower()
    return b if b in ("local", "nvidia") else "local"


def default_embedding_model() -> str:
    return NVIDIA_EMBEDDING_MODEL if embedding_backend() == "nvidia" else LOCAL_EMBEDDING_MODEL


# BGE-M3 can handle up to 8192 tokens natively.
# 900 chars is kept conservative for Devanagari density and memory.
EMBED_WINDOW_CHARS = int(os.getenv("PDFSPECS_EMBED_WINDOW_CHARS", "900"))
EMBED_WINDOW_OVERLAP = int(os.getenv("PDFSPECS_EMBED_WINDOW_OVERLAP", "90"))
# Windows per forward pass (local) / per request (nvidia).
EMBED_BATCH_SIZE = int(os.getenv("PDFSPECS_EMBED_BATCH_SIZE", "16"))
# Windows per NVIDIA embeddings request. This batching is why the API path is ~26x faster
# than local inference on this hardware despite the rate-limit throttle.
NVIDIA_EMBED_BATCH = int(os.getenv("PDFSPECS_NVIDIA_EMBED_BATCH", "32"))


@dataclass
class ProviderSettings:
    """Per-org NVIDIA NIM config for a single enrichment call (BYO key)."""
    nvidia_api_key: str = ""
    llm_model: str = NVIDIA_LLM_MODEL
    embedding_model: str = ""      # empty => resolve via default_embedding_model()

    def resolved_embedding_model(self) -> str:
        return self.embedding_model or default_embedding_model()


def _record_usage(usage_sink, purpose, prompt_tokens, completion_tokens):
    """Record token usage via the injected sink (org-scoped). No-op without a sink."""
    if not (prompt_tokens or completion_tokens) or usage_sink is None:
        return
    try:
        usage_sink(purpose, prompt_tokens, completion_tokens)
    except Exception as e:
        logger.error(f"Failed to record token usage: {e}")


def _post_with_retry(url, json_payload, headers, max_retries=5, backoff_factor=1.5):
    """Executes a POST request with global throttling + exponential backoff for transient errors."""
    for attempt in range(max_retries):
        try:
            _throttle()  # respect the provider's rate limit (every attempt counts)
            response = requests.post(url, json=json_payload, headers=headers, timeout=45)
            response.raise_for_status()
            return response
        except RequestException as e:
            # Surface the provider's explanation. requests' HTTPError message is only
            # "400 Client Error: Bad Request for url: ..." — the body carries the actual
            # reason ("Input length 576 exceeds maximum allowed token size 512"), and
            # without it a failed task is undiagnosable from its error_message alone.
            if isinstance(e, HTTPError) and e.response is not None:
                detail = (e.response.text or "")[:400].replace("\n", " ")
                if detail:
                    e = HTTPError(f"{e} :: {detail}", response=e.response,
                                  request=getattr(e, "request", None))

            if attempt == max_retries - 1:
                raise e

            # Don't retry on client errors (except 429 Too Many Requests).
            # `raise e`, not a bare `raise`: the latter re-raises the ORIGINAL exception and
            # throws away the response body attached above — which is the whole reason a
            # failed task's error_message said only "400 Bad Request for url".
            if isinstance(e, HTTPError) and e.response is not None:
                status = e.response.status_code
                if 400 <= status < 500 and status != 429:
                    raise e

            sleep_time = backoff_factor * (2 ** attempt)
            logger.warning(f"API request failed: {e}. Retrying in {sleep_time}s (Attempt {attempt + 1}/{max_retries})...")
            time.sleep(sleep_time)


# Keywords are deliberately NOT requested: chunk_keywords was write-only and has been
# dropped (migration 0007). Every keyword cost prompt output and an INSERT, and was
# never read back.
ENRICH_PROMPT = """Analyze the following text chunk from a document. Extract:
1. A concise 1-2 sentence summary of the key information.
2. Important entities mentioned (e.g. key books, references, people, organizations, locations, concepts). Entity names should be normalized to their canonical forms.

Return the output strictly in the following JSON format:
{{
  "summary": "...",
  "entities": [
    {{"name": "Entity Name", "type": "concept"}},
    {{"name": "Another Entity", "type": "person"}}
  ]
}}
Valid types for entities are: "concept", "organization", "person", "location".

Text chunk:
{text}
"""


def _call_nvidia_llm(text: str, api_key: str, model: str, usage_sink=None) -> dict:
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": ENRICH_PROMPT.format(text=text)}
        ],
        "response_format": {"type": "json_object"}
    }

    response = _post_with_retry(url, json_payload=payload, headers=headers)
    data = response.json()

    # Record token usage
    usage = data.get("usage", {})
    _record_usage(usage_sink, "ingestion",
                  usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    try:
        content_str = data["choices"][0]["message"]["content"]
        return json.loads(content_str)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Failed to parse Nvidia response: {e}. Raw response: {data}")
        raise ValueError("Invalid response format from Nvidia NIM API")


# Token budget per window. nv-embedqa-e5-v5 hard-caps at 512 and returns
# `400 Input length N exceeds maximum allowed token size 512` — a client error, so it is
# NOT retried and the whole document fails. Aim well under the cap.
EMBED_TOKEN_BUDGET = int(os.getenv("PDFSPECS_EMBED_TOKEN_BUDGET", "400"))

# Tokens-per-character, MEASURED against the live API (by reading the token count back out
# of its 400 responses) — not assumed. A fixed CHARACTER window can't work across scripts.
#
# The usual "4 chars per token" rule of thumb (0.25) is badly wrong for this corpus and
# cost two failed documents: the Latin-script text here is transliterated Sanskrit
# ("Saptalashankhini"), table markup and punctuation, which tokenizes at 0.478 t/c —
# nearly double plain English. Devanagari measured 0.64 t/c (900 chars -> 576 tokens).
# Both values below are rounded UP from the measured maxima to leave headroom.
_TOK_PER_CHAR_DEVANAGARI = 0.70   # measured max 0.64
_TOK_PER_CHAR_OTHER = 0.50        # measured max 0.480
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def _window_chars_for(text: str) -> int:
    """
    Characters per window that keep a window inside EMBED_TOKEN_BUDGET tokens.

    Scales with the text's Devanagari ratio: ~1600 chars for pure English, ~625 for pure
    Devanagari, interpolating for the mixed Sanskrit+English records in this corpus.
    """
    if not text:
        return EMBED_WINDOW_CHARS
    ratio = len(_DEVANAGARI_RE.findall(text)) / len(text)
    tok_per_char = (_TOK_PER_CHAR_DEVANAGARI * ratio) + (_TOK_PER_CHAR_OTHER * (1 - ratio))
    return max(200, int(EMBED_TOKEN_BUDGET / max(tok_per_char, 0.01)))


def _windows(text: str) -> list:
    """
    Split text into overlapping windows that each fit the embedding model's token cap.

    Empty/whitespace-only windows are dropped: the API rejects them outright with
    `400 ... all elements must be non-empty`, failing the entire batch.
    """
    text = text or ""
    if not text.strip():
        return []

    size = _window_chars_for(text)
    if len(text) <= size:
        return [text]

    overlap = min(EMBED_WINDOW_OVERLAP, size // 10)
    step = max(1, size - overlap)
    out = []
    for i in range(0, len(text), step):
        window = text[i:i + size]
        if window.strip():
            out.append(window)
        if i + size >= len(text):
            break
    return out


_embedding_model_instance = None
_embedding_model_lock = threading.Lock()

# BGE-M3 defaults to max_seq_length=8192. Our embedding windows are EMBED_WINDOW_CHARS
# (~900 chars ≈ 300 tokens), so the extra headroom buys nothing and costs a great deal of
# memory — on Apple Silicon it OOMs the MPS backend part-way through a document. Cap it to
# something that comfortably fits a window.
EMBED_MAX_SEQ_LEN = int(os.getenv("PDFSPECS_EMBED_MAX_SEQ_LEN", "512"))
# Force a device ("cpu" / "mps" / "cuda"); empty = let sentence-transformers choose.
EMBED_DEVICE = os.getenv("PDFSPECS_EMBED_DEVICE", "").strip()


def _get_embedding_model(model_name: str):
    global _embedding_model_instance
    if SentenceTransformer is None:
        raise ImportError("sentence-transformers is required for local embeddings")

    if _embedding_model_instance is None:
        with _embedding_model_lock:
            if _embedding_model_instance is None:
                logger.info("Loading local embedding model %s (device=%s, max_seq_len=%d)...",
                            model_name, EMBED_DEVICE or "auto", EMBED_MAX_SEQ_LEN)
                kwargs = {"device": EMBED_DEVICE} if EMBED_DEVICE else {}
                model = SentenceTransformer(model_name, **kwargs)
                model.max_seq_length = min(model.max_seq_length, EMBED_MAX_SEQ_LEN)
                _embedding_model_instance = model
    return _embedding_model_instance


def _release_accelerator_memory():
    """
    Drop cached accelerator allocations between batches.

    Without this, MPS holds freed blocks and fragments until a mid-document batch fails
    with 'MPS backend out of memory' — after several minutes of successful work.
    """
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# _call_local_embedding (single text) removed: embed_texts_batch handles one or many and
# dispatches on the configured backend, so there's one embedding entrypoint rather than two
# that could diverge.


def _embed_windows_nvidia(windows: list, api_key: str, model: str,
                          input_type: str = "passage", purpose: str = "ingestion",
                          usage_sink=None) -> list:
    """
    Embed a flat list of windows via the NIM embeddings API, NVIDIA_EMBED_BATCH per request.

    Returns ONE LIST OF VECTORS PER INPUT WINDOW, aligned 1:1 with `windows` — normally a
    single vector each, but more when a window had to be split to fit the token cap. The
    caller maps windows back to chunks positionally, so this alignment is load-bearing.
    """
    if not windows:
        return []
    if not api_key:
        raise APIKeyMissingError(
            "PDFSPECS_EMBEDDING_BACKEND=nvidia needs an NVIDIA API key configured for the org.")

    url = "https://integrate.api.nvidia.com/v1/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    groups = []
    for start in range(0, len(windows), NVIDIA_EMBED_BATCH):
        batch = windows[start:start + NVIDIA_EMBED_BATCH]
        groups.extend(_embed_one_batch(batch, url, headers, model, input_type,
                                       purpose, usage_sink))
    return groups


def _is_token_limit_error(exc) -> bool:
    """True for the API's 'Input length N exceeds maximum allowed token size 512' 400."""
    return isinstance(exc, HTTPError) and "maximum allowed token size" in str(exc)


def _embed_one_batch(batch: list, url, headers, model, input_type, purpose, usage_sink,
                     depth: int = 0) -> list:
    """
    Embed one request's worth of windows, halving over-long windows on a token-limit 400.

    Returns one list of vectors PER INPUT WINDOW (aligned 1:1 with `batch`), not a flat
    list — a window that had to be split contributes several vectors, and flattening them
    here destroys the caller's window-to-chunk mapping.

    The character-per-token ratios above are measured, but they are still an estimate — a
    passage that tokenizes worse than anything sampled would 400, and a 400 is a client
    error so it is never retried: the whole document fails. That is exactly how two files
    died mid-run. Splitting the offending windows and retrying makes the path self-healing,
    so a bad estimate costs a little retrieval granularity instead of a document.
    """
    try:
        response = _post_with_retry(url, json_payload={
            "input": batch, "model": model,
            "encoding_format": "float", "input_type": input_type,
        }, headers=headers)
    except Exception as exc:
        if not _is_token_limit_error(exc) or depth >= 4:
            raise
        # Halve every window in this batch and retry. Each half keeps its own vector, so
        # nothing is dropped — the chunk simply ends up with more, finer-grained windows.
        #
        # The halves stay GROUPED UNDER THE WINDOW THEY CAME FROM. Returning them flat
        # made this function hand back more vectors than it was given windows, and the
        # caller zips vectors against window owners: every vector after a split shifted
        # onto the wrong chunk and the tail was silently dropped, so chunks ended up
        # semantically indexed with a neighbour's text.
        halves_per_window = []
        for w in batch:
            if len(w) <= 80:
                halves_per_window.append([w])   # already tiny; splitting won't help
                continue
            mid = len(w) // 2
            halves_per_window.append([w[:mid], w[mid:]])

        flat = [h for halves in halves_per_window for h in halves]
        logger.warning("Embedding batch exceeded the token limit; splitting %d windows into "
                       "%d and retrying (depth %d).", len(batch), len(flat), depth + 1)
        half_groups = []
        for i in range(0, len(flat), NVIDIA_EMBED_BATCH):
            half_groups.extend(_embed_one_batch(flat[i:i + NVIDIA_EMBED_BATCH], url, headers,
                                                model, input_type, purpose, usage_sink,
                                                depth + 1))
        out, pos = [], 0
        for halves in halves_per_window:
            group = []
            for _ in halves:
                group.extend(half_groups[pos])
                pos += 1
            out.append(group)
        return out

    data = response.json()
    try:
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        vectors = [it["embedding"] for it in items]
    except (KeyError, IndexError) as e:
        logger.error(f"Failed to extract embedding from Nvidia: {e}. Raw response: {data}")
        raise ValueError("Invalid embedding response format from Nvidia NIM API")
    if len(vectors) != len(batch):
        # Positional alignment is the only thing tying a vector to its chunk.
        raise ValueError(f"NVIDIA embeddings returned {len(vectors)} vectors for "
                         f"{len(batch)} windows; refusing to guess the mapping.")
    _record_usage(usage_sink, purpose, data.get("usage", {}).get("prompt_tokens", 0), 0)
    return [[v] for v in vectors]


def embed_texts_batch(texts: list, model: str = None, settings: "ProviderSettings" = None,
                      input_type: str = "passage", purpose: str = "ingestion",
                      usage_sink=None) -> list:
    """
    Embed many chunk texts at once; returns [[vec, ...], ...] — one window-vector list per
    input text, in input order.

    Windowing every chunk up front and embedding the flattened list is what lets either
    backend fill its batches. Embedding per chunk instead meant hundreds of tiny,
    under-filled calls per document.

    Dispatches on PDFSPECS_EMBEDDING_BACKEND. Both backends are 1024-d.
    """
    if not texts:
        return []

    model = model or (settings.resolved_embedding_model() if settings
                      else default_embedding_model())

    per_text_windows = [_windows(t) for t in texts]
    flat, owners = [], []
    for i, windows in enumerate(per_text_windows):
        for w in windows:
            flat.append(w)
            owners.append(i)

    if not flat:
        return [[] for _ in texts]

    if embedding_backend() == "nvidia":
        per_window = _embed_windows_nvidia(
            flat, settings.nvidia_api_key if settings else "", model,
            input_type=input_type, purpose=purpose, usage_sink=usage_sink)
    else:
        embedder = _get_embedding_model(model)
        vectors = embedder.encode(flat, normalize_embeddings=True, batch_size=EMBED_BATCH_SIZE)
        _release_accelerator_memory()
        per_window = [[v] for v in vectors]

    # `owners` is positional, so a backend that returns a different number of groups than
    # we sent windows would misattribute every vector after the discrepancy.
    if len(per_window) != len(flat):
        raise ValueError(f"Embedding backend returned {len(per_window)} window groups for "
                         f"{len(flat)} windows; refusing to guess the mapping.")

    out = [[] for _ in texts]
    for owner, group in zip(owners, per_window):
        for vec in group:
            out[owner].append(vec.tolist() if hasattr(vec, "tolist") else list(vec))
    return out


_VALID_ENTITY_TYPES = {"concept", "organization", "person", "location"}


def _clean_metadata(raw) -> dict:
    """
    Normalize one LLM metadata object into {"summary": str, "entities": [...]}.

    LLM output is untrusted: fields may be missing, the wrong type, or (as seen from some
    models) a list of non-dict items — guard everything.
    """
    if not isinstance(raw, dict):
        return {"summary": "", "entities": []}

    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        summary = str(summary) if summary is not None else ""

    raw_entities = raw.get("entities", [])
    if not isinstance(raw_entities, list):
        raw_entities = []

    cleaned = []
    for ent in raw_entities:
        if not isinstance(ent, dict):
            continue  # skip malformed items (ints, bare strings, ...)
        name = ent.get("name", "")
        name = name.strip() if isinstance(name, str) else ""
        etype = ent.get("type", "")
        etype = etype.strip().lower() if isinstance(etype, str) else ""
        if etype not in _VALID_ENTITY_TYPES:
            etype = "concept"
        if name:
            cleaned.append({"name": name, "type": etype})

    return {"summary": summary, "entities": cleaned}


def extract_metadata(text: str, settings: ProviderSettings, usage_sink=None) -> dict:
    """
    Stage 2: the LLM call alone — summary + entities for one already-stored chunk.

    Split out of enrich_chunk so ingestion no longer waits on it. Keywords are no longer
    requested: chunk_keywords was write-only and has been dropped (migration 0007).

    Returns {"summary": str, "entities": [{"name", "type"}, ...]}.
    """
    if not settings.nvidia_api_key:
        raise APIKeyMissingError(
            "Nvidia API key is missing. Set it in Settings, or NVIDIA_API_KEY in the environment.")

    metadata = _call_nvidia_llm(text, settings.nvidia_api_key, settings.llm_model,
                                usage_sink=usage_sink)
    return _clean_metadata(metadata)


# Chunks per enrichment request. One call per chunk meant ~29,000 throttled calls (~12h)
# for this corpus; batching cuts that proportionally. Kept modest so a malformed response
# only costs one small batch, and so the prompt stays well inside the context window.
ENRICH_BATCH_SIZE = int(os.getenv("PDFSPECS_ENRICH_BATCH_SIZE", "8"))

ENRICH_BATCH_PROMPT = """You will be given {n} numbered text chunks from a document.
For EACH chunk, extract:
1. A concise 1-2 sentence summary of the key information.
2. Important entities mentioned (e.g. key books, references, people, organizations, \
locations, concepts). Entity names should be normalized to their canonical forms.

Return STRICTLY this JSON shape, with exactly {n} objects, one per chunk, in the same \
order, each carrying the chunk's "id":
{{
  "results": [
    {{"id": 1, "summary": "...", "entities": [{{"name": "Entity Name", "type": "concept"}}]}},
    {{"id": 2, "summary": "...", "entities": []}}
  ]
}}
Valid entity types are: "concept", "organization", "person", "location".
Summarize each chunk independently. Do not merge chunks or skip any id.

{chunks}
"""


def extract_metadata_batch(texts: list, settings: ProviderSettings, usage_sink=None) -> list:
    """
    Extract summary + entities for SEVERAL chunks in one LLM call.

    Returns a list aligned 1:1 with `texts`. The model is asked to echo each chunk's id so
    results can be realigned rather than trusted positionally — a model that drops or
    reorders an item would otherwise attach summaries to the wrong chunks, which is far
    worse than having none. Any batch whose response can't be reconciled raises, and the
    caller falls back to single-chunk calls.
    """
    if not texts:
        return []
    if not settings.nvidia_api_key:
        raise APIKeyMissingError(
            "Nvidia API key is missing. Set it in Settings, or NVIDIA_API_KEY in the environment.")
    if len(texts) == 1:
        return [extract_metadata(texts[0], settings, usage_sink=usage_sink)]

    blocks = "\n\n".join(f"--- CHUNK {i + 1} ---\n{t}" for i, t in enumerate(texts))
    prompt = ENRICH_BATCH_PROMPT.format(n=len(texts), chunks=blocks)

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {settings.nvidia_api_key}",
               "Content-Type": "application/json"}
    payload = {"model": settings.llm_model,
               "messages": [{"role": "user", "content": prompt}],
               "response_format": {"type": "json_object"}}

    response = _post_with_retry(url, json_payload=payload, headers=headers)
    data = response.json()
    usage = data.get("usage", {})
    _record_usage(usage_sink, "ingestion",
                  usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    try:
        parsed = json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise ValueError(f"Unparseable batch enrichment response: {e}")

    results = parsed.get("results") if isinstance(parsed, dict) else parsed
    if not isinstance(results, list):
        raise ValueError("Batch enrichment response has no 'results' list")

    # Realign by the echoed id; fall back to position only when ids are absent entirely.
    by_id = {}
    for item in results:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            by_id[item["id"]] = item

    if by_id:
        missing = [i + 1 for i in range(len(texts)) if (i + 1) not in by_id]
        if missing:
            raise ValueError(f"Batch enrichment omitted chunk ids {missing[:5]}")
        return [_clean_metadata(by_id[i + 1]) for i in range(len(texts))]

    if len(results) != len(texts):
        raise ValueError(
            f"Batch enrichment returned {len(results)} results for {len(texts)} chunks")
    return [_clean_metadata(r) for r in results]


def embed_query(query: str, settings: ProviderSettings, usage_sink=None) -> list | None:
    """
    Embed a search query as a SINGLE vector.

    `input_type="query"` is what makes asymmetric retrieval work — the model embeds
    questions and passages into the same space from different sides. A query long enough
    to window is rare; if it happens we keep the first window rather than averaging, since
    the leading text carries the intent.
    """
    out = embed_texts_batch([query], settings=settings, input_type="query",
                            purpose="sandbox", usage_sink=usage_sink)
    vectors = out[0] if out else []
    return vectors[0] if vectors else None
