-- Switch the platform embedding dimension from 768 (Gemini text-embedding-004) to
-- 1024 (NVIDIA nv-embedqa-e5-v5). Safe to run while chunks is empty.
-- The HNSW index must be dropped before altering the column type, then recreated.
drop index if exists idx_chunks_embedding;

alter table chunks alter column embedding type vector(1024);

create index if not exists idx_chunks_embedding
    on chunks using hnsw (embedding vector_cosine_ops);
