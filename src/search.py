"""
Org-scoped hybrid search — the single implementation used by both the REST API and the
MCP tools (previously duplicated across cli.py and mcp_server.py; FIXPLAN 4.1).

Fuses Postgres full-text (lexical) and pgvector similarity (semantic) with Reciprocal
Rank Fusion (FIXPLAN 3.3): both ranked lists contribute 1/(k + rank), which is scale-free
and fixes the old cosine-vs-reciprocal-rank mismatch. The query embedding is produced with
the owning org's provider key (BYO); with no key configured, search degrades to lexical-only.
"""
import logging

import src.pgdb as pgdb
import src.enricher as enricher

logger = logging.getLogger("pdfspecs.search")

# RRF dampening constant (standard default). Larger => flatter contribution by rank.
RRF_K = 60


def _org_settings(org_id: str) -> enricher.ProviderSettings:
    provider = pgdb.get_setting(org_id, "provider") or "gemini"
    return enricher.ProviderSettings(
        provider=provider,
        gemini_api_key=pgdb.get_setting(org_id, "gemini_api_key", decrypt=True),
        nvidia_api_key=pgdb.get_setting(org_id, "nvidia_api_key", decrypt=True),
        embedding_model=enricher.default_embedding_model(provider),
    )


def _embed_query(org_id: str, query: str):
    """Embed the query with the org's provider key; None if unavailable/fails."""
    s = _org_settings(org_id)

    def sink(purpose, p, c):
        pgdb.record_token_usage(org_id, "sandbox", p, c)

    try:
        if s.provider == "gemini" and s.gemini_api_key:
            return enricher._call_gemini_embedding(query, s.gemini_api_key, s.embedding_model)
        if s.provider == "nvidia" and s.nvidia_api_key:
            return enricher._call_nvidia_embedding(
                query, s.nvidia_api_key, s.embedding_model,
                input_type="query", purpose="sandbox", usage_sink=sink)
    except Exception as e:
        logger.error("Query embedding failed (falling back to lexical): %s", e)
    return None


def hybrid_search(org_id: str, query: str, limit: int = 5) -> list:
    """
    Return up to `limit` chunk rows for the org, ranked by RRF over lexical + semantic.
    Each row: {id, page_start, page_end, summary, text_content, filename, score}.
    """
    pool = max(limit * 2, limit)
    lexical = pgdb.search_lexical(org_id, query, limit=pool)

    semantic = []
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
