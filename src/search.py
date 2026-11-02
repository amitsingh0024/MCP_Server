"""
Org-scoped hybrid search — the single implementation used by both the REST API and the
MCP tools (previously duplicated across cli.py and mcp_server.py; FIXPLAN 4.1).

Fuses Postgres full-text (lexical) and pgvector similarity (semantic) with Reciprocal
Rank Fusion (FIXPLAN 3.3): both ranked lists contribute 1/(k + rank), which is scale-free
and fixes the old cosine-vs-reciprocal-rank mismatch. The query embedding is produced with
the owning org's NVIDIA key (BYO); with no key configured, search degrades to lexical-only.
"""
import logging

import src.pgdb as pgdb
import src.enricher as enricher

logger = logging.getLogger("pdfspecs.search")

# RRF dampening constant (standard default). Larger => flatter contribution by rank.
RRF_K = 60

# Bounds on `limit`. It arrives from a model (an MCP tool argument) or straight out of a
# request body, and feeds the candidate pool below — `limit * 10` rows from BOTH the
# lexical and the vector index. Unbounded, a single request asks Postgres for millions of
# candidate rows, so the clamp lives here, at the one chokepoint every caller goes through,
# rather than in each transport.
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50


def clamp_limit(value) -> int:
    """Coerce an untrusted `limit` into 1..MAX_SEARCH_LIMIT (the default on junk input)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SEARCH_LIMIT
    return max(1, min(n, MAX_SEARCH_LIMIT))


def _org_settings(org_id: str) -> enricher.ProviderSettings:
    return enricher.ProviderSettings(
        nvidia_api_key=pgdb.get_setting(org_id, "nvidia_api_key", decrypt=True),
    )


# Cache of org_id -> whether the active query model matches the stored corpus. Checked
# once per process rather than on every search.
_model_match_cache: dict = {}


def _query_model_matches_corpus(org_id: str) -> bool:
    """
    True when the ACTIVE embedding model matches the one the corpus was embedded with.

    Vectors from two different embedding models are not comparable — cosine similarity
    between them is meaningless — but both current models are 1024-d, so Postgres accepts
    the mismatch and search silently returns noise. This happened for real: the corpus was
    embedded with nv-embedqa-e5-v5 while .env still selected the local bge-m3 backend, so
    every semantic result was garbage and only lexical search kept results looking sane.
    """
    if org_id in _model_match_cache:
        return _model_match_cache[org_id]

    active = enricher.default_embedding_model()
    corpus = pgdb.corpus_embedding_models(org_id)
    ok = (not corpus) or (active in corpus)
    if not ok:
        logger.error(
            "Embedding model mismatch — SEMANTIC SEARCH DISABLED. Queries would be embedded "
            "with %r but this corpus was embedded with %s. Vectors from different models are "
            "not comparable. Set PDFSPECS_EMBEDDING_BACKEND to the backend that produced the "
            "corpus, or re-ingest. Falling back to lexical-only search.",
            active, ", ".join(repr(c) for c in corpus))
    _model_match_cache[org_id] = ok
    return ok


def _embed_query(org_id: str, query: str):
    """
    Embed the query for the semantic half of the search; None if unavailable/fails.

    The NVIDIA key is required by the `nvidia` embedding backend ONLY. The default
    `local` backend (BAAI/bge-m3 via sentence-transformers) runs entirely offline, and
    gating it on a key meant every local-first install — the whole point of the stdio MCP
    server — silently degraded to lexical-only search with nothing in the logs to say so.
    """
    s = _org_settings(org_id)
    if enricher.embedding_backend() == "nvidia" and not s.nvidia_api_key:
        logger.warning(
            "PDFSPECS_EMBEDDING_BACKEND=nvidia but this org has no NVIDIA key configured — "
            "falling back to lexical-only search. Set one (cli `set-key`, or the dashboard), "
            "or use PDFSPECS_EMBEDDING_BACKEND=local for offline embeddings.")
        return None

    def sink(purpose, p, c):
        pgdb.record_token_usage(org_id, "sandbox", p, c)

    try:
        return enricher.embed_query(query, s, usage_sink=sink)
    except Exception as e:
        logger.error("Query embedding failed (falling back to lexical): %s", e)
    return None


def hybrid_search(org_id: str, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list:
    """
    Return up to `limit` chunk rows for the org, ranked by RRF over lexical + semantic.
    Each row: {id, page_start, page_end, summary, text_content, filename, score}.

    `limit` is clamped here rather than trusted: see clamp_limit above.
    """
    limit = clamp_limit(limit)

    # RRF needs a DEEP candidate pool from each list to find agreement between them: a
    # document ranked ~#12 lexically and ~#11 semantically is a strong hybrid signal, and
    # a pool of limit*2 (10 at the default limit) never sees it.
    pool = max(50, limit * 10)
    lexical = pgdb.search_lexical(org_id, query, limit=pool)

    # Only run the semantic half when the query model matches the corpus. A mismatch
    # returns confident nonsense; lexical-only is degraded but honest.
    semantic = []
    if _query_model_matches_corpus(org_id):
        qvec = _embed_query(org_id, query)
        if qvec is not None:
            semantic = pgdb.search_semantic(org_id, qvec, limit=pool)

    # Reciprocal Rank Fusion over both ranked lists.
    scores: dict[str, float] = {}
    for rank, hit in enumerate(lexical):
        scores[hit["chunk_id"]] = scores.get(hit["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, hit in enumerate(semantic):
        scores[hit["chunk_id"]] = scores.get(hit["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)

    if not scores:
        return []

    top_ids = sorted(scores, key=scores.get, reverse=True)[:limit]
    rows = pgdb.get_chunks_by_ids(org_id, top_ids)
    results = []
    for cid in top_ids:
        row = rows.get(cid)
        if row:
            row["score"] = round(scores[cid], 6)
            results.append(row)
    return results
