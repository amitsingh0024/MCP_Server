-- REVAMP A-9 — Google Gemini is removed platform-wide; NVIDIA NIM is the only provider.
--
-- Two reasons this migration exists rather than just deleting code:
--   1. Orgs carry a saved `provider` setting. Any row still reading 'gemini' would send
--      requests to a code path that no longer exists.
--   2. Saved model names like 'gemini-2.5-flash' / 'text-embedding-004' would be posted
--      verbatim to the NVIDIA NIM API and 404. Clearing them lets the code defaults
--      (meta/llama-3.1-8b-instruct, nvidia/nv-embedqa-e5-v5) take over.

-- Encrypted Gemini keys are now unusable; don't keep secrets we can't use.
delete from org_settings where key = 'gemini_api_key';

-- `provider` is no longer read by the application, but leave no misleading value behind.
update org_settings set value = 'nvidia' where key = 'provider';

-- Drop stale Gemini-era model names so the NVIDIA defaults apply.
delete from org_settings
 where key in ('llm_model', 'embedding_model')
   and (value like 'gemini%' or value = 'text-embedding-004');
