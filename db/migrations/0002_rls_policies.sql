-- OpenPDFSpecs — Row-Level Security policies (M1)
-- SUPABASE-ONLY: these reference auth.uid(), which exists in Supabase's auth schema.
-- Run this AFTER 0001_init_schema.sql, in the Supabase SQL editor.
--
-- Enforcement model (see MULTITENANT_PLAN.md): the FastAPI backend connects with the
-- service role and filters every query by org_id itself (primary guard). RLS here is a
-- BACKSTOP that constrains any direct Supabase client access (anon key + a user's JWT)
-- to that user's own org. The service role bypasses RLS by design.

-- Resolve the caller's org from their auth uid. SECURITY DEFINER so the lookup on
-- orgs works regardless of the caller's own row visibility; search_path pinned.
create or replace function current_org_id() returns uuid
    language sql
    stable
    security definer
    set search_path = public
as $$
    select id from orgs where owner_auth_id = auth.uid()
$$;

-- --- Enable RLS -------------------------------------------------------
alter table orgs           enable row level security;
alter table documents      enable row level security;
alter table chunks         enable row level security;
alter table entities       enable row level security;
alter table chunk_entities enable row level security;
alter table chunk_keywords enable row level security;
alter table tasks          enable row level security;
alter table org_settings   enable row level security;
alter table api_keys       enable row level security;
alter table token_usage    enable row level security;

-- --- orgs: a user sees/edits only their own org ----------------------
drop policy if exists orgs_self on orgs;
create policy orgs_self on orgs
    for all
    using (owner_auth_id = auth.uid())
    with check (owner_auth_id = auth.uid());

-- --- Tenant tables: rows must belong to the caller's org -------------
drop policy if exists documents_isolation on documents;
create policy documents_isolation on documents
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists chunks_isolation on chunks;
create policy chunks_isolation on chunks
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists entities_isolation on entities;
create policy entities_isolation on entities
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists chunk_entities_isolation on chunk_entities;
create policy chunk_entities_isolation on chunk_entities
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists chunk_keywords_isolation on chunk_keywords;
create policy chunk_keywords_isolation on chunk_keywords
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists tasks_isolation on tasks;
create policy tasks_isolation on tasks
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists org_settings_isolation on org_settings;
create policy org_settings_isolation on org_settings
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists api_keys_isolation on api_keys;
create policy api_keys_isolation on api_keys
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());

drop policy if exists token_usage_isolation on token_usage;
create policy token_usage_isolation on token_usage
    for all using (org_id = current_org_id()) with check (org_id = current_org_id());
