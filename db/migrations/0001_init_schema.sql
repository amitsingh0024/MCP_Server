-- OpenPDFSpecs — Multi-tenant schema (M1)
-- Portable Postgres DDL. Safe to run on Supabase or a plain Postgres 14+ instance.
-- RLS policies live in 0002_rls_policies.sql (Supabase-specific: they reference auth.uid()).
--
-- Every tenant table carries org_id and cascades from orgs, so deleting an org
-- removes all of its data. Application code MUST additionally filter every query
-- by org_id (RLS is a backstop, not the primary guard — see MULTITENANT_PLAN.md).

-- pgvector for embedding similarity search.
create extension if not exists vector;

-- =====================================================================
-- Tenancy root
-- =====================================================================
-- One org per Google account (owner_auth_id maps to Supabase auth.users.id).
create table if not exists orgs (
    id            uuid primary key default gen_random_uuid(),
    owner_auth_id uuid not null unique,          -- Supabase auth.uid()
    name          text not null default 'My Organization',
    created_at    timestamptz not null default now()
);

-- =====================================================================
-- Documents
-- =====================================================================
create table if not exists documents (
    id         uuid primary key default gen_random_uuid(),
    org_id     uuid not null references orgs(id) on delete cascade,
    filename   text not null,
    sha256     text not null,
    size_bytes bigint not null,
    created_at timestamptz not null default now(),
    -- sha256 is unique PER ORG: two orgs may independently ingest the same PDF.
    unique (org_id, sha256)
);
create index if not exists idx_documents_org on documents(org_id);

-- =====================================================================
-- Chunks (embedding + full-text)
-- =====================================================================
-- Embedding dimension is fixed platform-wide (Gemini text-embedding-004 = 768-d);
-- see MULTITENANT_PLAN.md. embedding_model is kept for traceability only.
-- tsv is a generated column, so lexical search stays in sync automatically
-- (this structurally removes the old FTS5 dedupe/sync bugs).
create table if not exists chunks (
    id              uuid primary key default gen_random_uuid(),
    org_id          uuid not null references orgs(id) on delete cascade,
    document_id     uuid not null references documents(id) on delete cascade,
    page_start      int not null,
    page_end        int not null,
    text_content    text not null,
    summary         text,
    embedding       vector(768),
    embedding_model text,
    tsv             tsvector generated always as (
        to_tsvector('simple',
            coalesce(text_content, '') || ' ' || coalesce(summary, ''))
    ) stored,
    created_at      timestamptz not null default now()
);
create index if not exists idx_chunks_org      on chunks(org_id);
create index if not exists idx_chunks_document on chunks(document_id);
-- Lexical search index (GIN over the generated tsvector).
create index if not exists idx_chunks_tsv on chunks using gin(tsv);
-- Vector similarity index (HNSW, cosine). Query filters by org_id then orders by <=>.
create index if not exists idx_chunks_embedding
    on chunks using hnsw (embedding vector_cosine_ops);

-- =====================================================================
-- Entity graph
-- =====================================================================
create table if not exists entities (
    id      text not null,          -- canonical "type:name"
    org_id  uuid not null references orgs(id) on delete cascade,
    name    text not null,
    type    text not null,
    primary key (org_id, id)
);

create table if not exists chunk_entities (
    org_id    uuid not null references orgs(id) on delete cascade,
    chunk_id  uuid not null references chunks(id) on delete cascade,
    entity_id text not null,
    primary key (org_id, chunk_id, entity_id),
    foreign key (org_id, entity_id) references entities(org_id, id) on delete cascade
);
create index if not exists idx_chunk_entities_entity on chunk_entities(org_id, entity_id);

create table if not exists chunk_keywords (
    org_id   uuid not null references orgs(id) on delete cascade,
    chunk_id uuid not null references chunks(id) on delete cascade,
    keyword  text not null,
    primary key (org_id, chunk_id, keyword)
);

-- =====================================================================
-- Ingestion queue
-- =====================================================================
-- storage_key is the Supabase Storage object path (replaces the old local file_path).
create table if not exists tasks (
    id                bigint generated always as identity primary key,
    org_id            uuid not null references orgs(id) on delete cascade,
    document_id       uuid references documents(id) on delete set null,
    storage_key       text not null,
    original_filename text,
    status            text not null check (status in ('pending','processing','completed','failed')),
    attempts          int not null default 0,
    error_message     text,
    progress_info     text default '',
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists idx_tasks_org_status on tasks(org_id, status);

-- Keep updated_at fresh on any row change.
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_tasks_updated_at on tasks;
create trigger trg_tasks_updated_at
    before update on tasks
    for each row execute function set_updated_at();

-- =====================================================================
-- Per-org settings (provider, models, encrypted provider key)
-- =====================================================================
-- Secret values (e.g. gemini_api_key) are Fernet-encrypted by the app before insert.
create table if not exists org_settings (
    org_id uuid not null references orgs(id) on delete cascade,
    key    text not null,
    value  text not null,
    primary key (org_id, key)
);

-- =====================================================================
-- Agent API keys (org-scoped). Only the hash is stored; raw shown once.
-- =====================================================================
create table if not exists api_keys (
    token_hash  text primary key,   -- sha256 hex of the raw sk-pdfspecs-... token
    org_id      uuid not null references orgs(id) on delete cascade,
    description text not null,
    created_at  timestamptz not null default now()
);
create index if not exists idx_api_keys_org on api_keys(org_id);

-- =====================================================================
-- Token usage metrics (per org)
-- =====================================================================
create table if not exists token_usage (
    id                bigint generated always as identity primary key,
    org_id            uuid not null references orgs(id) on delete cascade,
    purpose           text not null check (purpose in ('ingestion','sandbox')),
    prompt_tokens     int not null default 0,
    completion_tokens int not null default 0,
    total_tokens      int not null default 0,
    created_at        timestamptz not null default now()
);
create index if not exists idx_token_usage_org on token_usage(org_id, purpose);
