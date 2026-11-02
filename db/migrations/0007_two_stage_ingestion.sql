-- REVAMP Phase E — two-stage ingestion.
--
-- Ingestion used to be one pass: parse → chunk → enrich (one LLM call per chunk) → store.
-- Nothing was written until every chunk had been enriched, so a corpus of ~13,500 chunks
-- meant ~5.6 hours of throttled API calls before a single passage became searchable, and
-- a failure anywhere threw the whole document away.
--
-- Since embeddings moved local (Phase C), the search-critical path — parse, chunk, embed,
-- store — makes NO network calls at all. Splitting the stages lets it run at full speed:
--   stage 1 (store):  chunks + embeddings land immediately; fully searchable.
--   stage 2 (enrich): summaries + entities backfilled afterwards, resumable.
--
-- `enriched_at` is the marker. NULL = stage 2 still owes this chunk.

alter table chunks add column if not exists enriched_at timestamptz;

-- Partial index: the enrichment pass only ever asks for the NULL rows, and once a corpus
-- is fully enriched this index costs nothing to keep.
create index if not exists idx_chunks_pending_enrichment
    on chunks (org_id) where enriched_at is null;

-- Chunks ingested before this migration already carry a summary from the old single-pass
-- flow; don't re-enrich them.
update chunks set enriched_at = created_at
 where enriched_at is null and summary is not null and summary <> '';

-- =====================================================================
-- Drop chunk_keywords — write-only since it was introduced
-- =====================================================================
-- pgdb.add_keyword INSERTs into this table and NOTHING has ever read from it: no SELECT,
-- no JOIN, no tool, no endpoint. Every keyword the LLM extracted cost a third of the
-- enrichment prompt's output plus one INSERT round-trip per keyword, then sat unread.
-- Entity lookup (the feature people assume this powers) goes through entities /
-- chunk_entities, which are untouched.
--
-- Derived data with no readers — regenerable from the source PDFs if ever needed.
drop table if exists chunk_keywords;
