# OpenPDFSpecs — Multi-Tenant SaaS Migration Plan

Turns the local-first, single-user app into a hosted, multi-org RAG service.
**Delete this file once the migration is complete.**

## Locked decisions (from user)
- **BYO provider keys** — each org sets its own LLM/embedding API key. Cost & rate
  limits isolate automatically; the platform never pays for an org's usage.
- **Stack chosen for us** (see below); user reviews before we commit code.
- **1 Google account = 1 org.** The logging-in user owns exactly one org. Colleagues
  do NOT log in — they consume the MCP/API using the org's API key only.

## Two principals (keep these straight)
1. **Admin** — Google login → web dashboard. Ingests PDFs, sets provider key, mints API keys. Auth = Supabase JWT.
2. **MCP consumer** — colleague's Claude/Cursor. No login. Auth = org API key → resolves to `org_id`.

Google auth guards the dashboard; the API key guards the MCP/API surface. Both resolve to an `org_id`, and **every** data row/query is scoped by `org_id`.

---

## Chosen stack (my picks — review these)
| Layer | Choice | Why |
|---|---|---|
| DB + vector | **Supabase Postgres + pgvector** (free) | One platform for DB, vector, auth, storage, RLS |
| Lexical search | Postgres **`tsvector`/`tsquery`** | Replaces SQLite FTS5 |
| Auth (admin) | **Supabase Auth** (Google provider) | Built-in Google OAuth, issues JWT the backend verifies |
| File storage | **Supabase Storage** (bucket per-path `org_id/...`) | Ephemeral hosts wipe local disk |
| Isolation | Postgres **Row-Level Security** + app-layer `org_id` filter | Defense in depth |
| Backend | **Render** Docker web service (free) | Persistent process for the worker + Tesseract lang packs (serverless can't); note: sleeps when idle → cold starts |
| Frontend | **Vercel** (free) | Natural home for the existing Next.js app |
| MCP transport | **Streamable HTTP / SSE** (not stdio) | Remote clients can't use stdio |

**Alternatives if a free tier disappoints:** Fly.io or Railway for the backend; Neon for Postgres. Render's idle-sleep is the main free-tier wart — first MCP call after inactivity is slow.

### ⚠️ Critical design note — one embedding model platform-wide
`pgvector` columns are **fixed-dimension**. Orgs bringing different embedding providers
(Gemini 768-d vs NVIDIA 1024-d) can't share one `vector(N)` column. **Resolution:** the
platform **standardizes on ONE embedding model** (proposed: Gemini `text-embedding-004`,
768-d) so the vector column is uniform. Orgs still BYO *key* for that provider. LLM
*synthesis* provider can stay flexible per org; only the embedding model is fixed.
(This simplifies the dim-tracking work from Phase 1.2 — dim is now constant.)

---

## Target data model (Postgres, all tenant-scoped)
- `orgs (id uuid pk, owner_auth_id text unique, name, created_at)`
- `documents (id, org_id fk, filename, sha256, size_bytes, created_at)` — unique `(org_id, sha256)`
- `chunks (id, org_id, document_id, page_start, page_end, text_content, summary, embedding vector(768), tsv tsvector, created_at)`
- `entities (id, org_id, name, type)`, `chunk_entities`, `chunk_keywords` (all carry `org_id`)
- `tasks (id, org_id, file_path/storage_key, status, attempts, error_message, progress_info, timestamps)`
- `org_settings (org_id, key, value)` — provider, models, **encrypted** provider key
- `api_keys (token_hash text pk, org_id, description, created_at)` — store only the hash
- `token_usage (id, org_id, purpose, prompt/completion/total tokens, ts)`

Indexes: pgvector HNSW/IVFFlat on `chunks.embedding`; GIN on `chunks.tsv`; btree on every `org_id`.
RLS: enabled on all tenant tables; policies key off the request's org context.

---

## Migration phases (execute one at a time, like Phase 1)

- [~] **M0 — Provision & scaffold.** Supabase project live (ap-northeast-1, PG 17.6);
      `vector` extension enabled; both migrations applied; `.env` filled with new-model keys
      (secret/publishable/JWKS) + working `DATABASE_URL`. `.env.example` + checklist done.
      *Still user-side (for later milestones):* Google OAuth creds (M3), storage bucket (M4),
      Fernet key, hosting accounts (M7).
- [x] **M1 — Postgres schema + tenancy + RLS.** Done: `db/migrations/0001_init_schema.sql`
      (tables, pgvector `vector(768)`, generated `tsvector`, org_id + cascades, composite
      entity FK, HNSW/GIN indexes) and `0002_rls_policies.sql` (RLS + `current_org_id()`).
      *Verified* on a local Postgres 16-equivalent (pgvector bits shimmed): both migrations
      apply; generated tsvector matches; per-org sha256 uniqueness; cross-org entity FK
      blocked; cascade delete isolates tenants; RLS enabled on all 10 tenant tables.
- [~] **M2 — Data layer rewrite.** New Postgres layer `src/pgdb.py` (psycopg3 pool +
      pgvector): every function org-scoped; lexical search via `websearch_to_tsquery`
      (accepts arbitrary input — no sanitizer needed); semantic search via pgvector `<=>`
      in-DB against the HNSW index (no more loading all embeddings into Python); queue
      dequeue via `FOR UPDATE SKIP LOCKED`; encrypted per-org settings; hashed API keys →
      org_id; per-org metrics. Deps added (`psycopg[binary]`, `psycopg-pool`, `pgvector`).
      *Verified* against local Postgres (pgvector shimmed): all functions pass incl.
      cross-org isolation for docs/chunks/lexical/entities/settings/keys/metrics.
      *Validated on live Supabase (PG 17.6):* connection + pool (`prepare_threshold=None`
      for the pooler), all 10 tables, and `search_semantic` real pgvector `<=>` nearest
      neighbor + `tsvector` lexical — all pass.
      *Not yet wired:* callsites (queue/mcp/server/cli) still use the old SQLite `src/db.py`;
      they move onto `pgdb` in M5/M6 once orgs exist (needs the M3 auth/org bootstrap first).
- [~] **M3 — Admin auth.** Backend layer done: `src/auth.py` (transport-agnostic).
      - Supabase user JWT verified via JWKS (`verify_supabase_jwt`); expired / wrong-audience
        / tampered / empty all rejected.
      - `authenticate_admin` bootstraps the caller's org on first login (idempotent).
      - Org API keys: `generate_api_key` (raw `sk-pdfspecs-...` shown once) / `resolve_api_key`
        → org_id; only the SHA-256 **hash** is stored (folds in FIXPLAN 2.2/2.3).
      - Google provider enabled in Supabase; `pyjwt` added.
      *Verified:* full unit suite (mocked JWKS) + live-DB org bootstrap + key roundtrip pass.
      *Remaining:* FastAPI dependency wrappers + endpoints (in the M5/M6 server rewrite) and
      the frontend Google-login UI + dashboard shell.
- [x] **M4 — File storage.** Done: `src/storage.py` (Supabase Storage REST API, no SDK).
      `upload_bytes` → `<org_id>/<uuid>-<safe-filename>`; `download_to_path` for the worker;
      `delete_object`; `safe_filename` strips path traversal. Private `pdf-uploads` bucket
      created (PDF-only, 50 MB cap). *Verified* live round-trip: sanitize → upload → download
      (byte-exact) → delete → confirm gone.
      *In M5:* wire the API upload endpoint to enforce the configured `max_upload_mb`
      (FIXPLAN 3.4) before calling `upload_bytes`, and have the worker pull via `download_to_path`.
- [x] **M5 — Ingestion/worker on hosting.** Done: `src/ingest.py` (org-scoped worker) +
      `enricher.py` refactor (accepts a per-org `ProviderSettings` + injectable `usage_sink`,
      backward-compatible). Pipeline: download from storage → parse → diff-skip → chunk →
      **parallel enrich** (ThreadPoolExecutor, FIXPLAN 3.1) with the org's BYO key → store
      chunks/entities/keywords org-scoped; token usage recorded per org. Retry-with-cap
      (FIXPLAN 3.5). OCR pipeline unchanged (Tesseract packs go in the Docker image at M7).
      *Verified* live (DB + bucket, enrichment stubbed to avoid API cost): real PDF → 65
      chunks/entities/keywords; search + org metrics; diff-skip; fail-at-cap cleans partial
      doc; retry requeues. All pass.
- [x] **M6 — HTTP service + MCP (org-scoped).** Done.
      - `src/search.py`: single hybrid search with proper RRF (FIXPLAN 3.3) shared by REST +
        MCP (de-dups the old cli/mcp copies, FIXPLAN 4.1); query embedded with the org's key.
      - `src/server.py` rewritten on pgdb/auth/storage/ingest/search. Two deps: `require_admin`
        (Supabase JWT→org) and `require_api_key` (org key→org). Org-scoped endpoints: me, config
        (BYO key), documents, tasks, ingest (enforces `max_upload_mb`, FIXPLAN 3.4), metrics,
        API-key CRUD, search. Agent REST search via API key. Lifespan starts the ingest worker
        (FIXPLAN 4.5). CORS from env.
      - MCP tools (`list_documents`/`search_knowledge`/`retrieve_chunk`/`entity_lookup`) scoped
        by org via a pure-ASGI `MCPAuthMiddleware` (API key → contextvar).
      - Removed the dead SQLite modules: db.py, queue.py, mcp_server.py, cli.py (+ their tests).
      *Verified* live: unauth 401 on REST + `/mcp`; admin endpoints org-scoped; agent API-key
      search with cross-org isolation; bad key 401.
      *Deferred:* full MCP SSE handshake needs a live client (M7).
- [~] **M7 — Deploy.** Backend containerization done: `Dockerfile` (python:3.12-slim +
      tesseract eng/hin/san), `.dockerignore`, `render.yaml` (free Docker web service,
      Singapore, `/health` check, env-var secrets). `crypto_utils` now takes the Fernet key
      from `PDFSPECS_FERNET_KEY` env (was a local file — would regenerate per deploy on an
      ephemeral container and break decryption). Public bind via `$PORT`; CORS from env
      (inverts FIXPLAN 2.1/2.4). *Verified locally:* the exact container CMD boots against the
      live DB (`/health` 200, unauth 401, worker starts); env-based encrypt/decrypt roundtrip.
      `docs/M7_DEPLOY.md` checklist written.
      *User-side:* push repo to GitHub, create the Render service, set env vars, deploy.
      *Then:* frontend (Vercel) — Google-login UI + dashboard; live MCP-client test.
- [ ] **M8 — Hardening.** Per-org rate limits/quotas; automated cross-tenant isolation
      tests (org A can never read org B); portable tests; remove local-first artifacts.

## Relationship to the existing FIXPLAN
- **Phase 1 (done):** carries over — OCR, dim-tracking, FTS hardening, dedupe all still apply.
- **Phase 2:** folds into M3 (2.2/2.3 token security) and M7 (2.1/2.4 bind/CORS **inverted**). Old Phase 2 is superseded — do NOT execute it as originally written.
- **Phase 3/4 items still relevant:** RRF (3.3), de-dup search logic (4.1), drop `google-genai` (4.2), unify model name (4.3), logging cleanup (4.4), lifespan (4.5). Reapply on the Postgres code.

## Open questions to settle before M1
1. Confirm the fixed embedding model (proposed **Gemini text-embedding-004 / 768-d**).
2. Backend RLS enforcement: run queries under the org's Postgres role (`SET LOCAL app.current_org`) for true RLS, or rely on app-layer `org_id` filters with RLS as backstop? (I recommend app-layer filter first, RLS backstop, upgrade later.)
3. Provider-key encryption: reuse Fernet with the key sourced from an env var (`PDFSPECS_FERNET_KEY`)?
