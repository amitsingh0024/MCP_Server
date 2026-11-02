-- REVAMP B-2 — Multi-vector embeddings.
--
-- A chunk longer than one embedding window used to be collapsed into a single mean-pooled
-- vector, capped at 8 windows. Two consequences:
--   * content past ~8000 chars was never embedded at all — present in the DB, unreachable
--     by semantic search;
--   * averaging diluted whichever window actually matched the query, so the more content
--     a chunk held, the LESS findable each part of it became.
--
-- Instead we store one row per window and score a chunk by its best-matching window
-- (max-sim), which is what search_semantic now does.
--
-- DESTRUCTIVE: drops chunks.embedding. Every existing vector is already invalid — they
-- were either produced by the removed Gemini path or dropped at write time by the
-- 768-vs-1024 dimension bug. Re-ingestion is required either way.

create table if not exists chunk_embeddings (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null references orgs(id)   on delete cascade,
    chunk_id     uuid not null references chunks(id) on delete cascade,
    window_index int  not null,
    embedding    vector(1024) not null,
    -- Re-embedding a chunk overwrites its windows in place (add_chunk upserts on this).
    unique (chunk_id, window_index)
);

create index if not exists idx_chunk_embeddings_org   on chunk_embeddings(org_id);
create index if not exists idx_chunk_embeddings_chunk on chunk_embeddings(chunk_id);
create index if not exists idx_chunk_embeddings_vec
    on chunk_embeddings using hnsw (embedding vector_cosine_ops);

-- Superseded by the table above. The cascade from chunks keeps delete_document working
-- unchanged, so no application change is needed for cleanup.
drop index if exists idx_chunks_embedding;
alter table chunks drop column if exists embedding;
