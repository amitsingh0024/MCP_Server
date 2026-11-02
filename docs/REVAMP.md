# OpenPDFSpecs — Revamp

Phase A (NVIDIA-only + Tier 0 accuracy) below; **Phase B (local-first) is documented at the
bottom of this file.**

---

# Phase A — NVIDIA-only + Tier 0 accuracy

Detailed change specification. Written **before** implementation so the intent, the
blast radius, and the verification steps are all reviewable up front.

> ## Status
>
> **Code: complete.** All of Part A and Part B is implemented; 14 unit tests pass, every
> module imports, and the frontend typechecks clean (`npx tsc --noEmit`).
>
> **Migrations: applied LOCALLY only.** `0004`–`0006` have been run against the new local
> `pdfspecs` database (Phase B) via `python -m src.migrate`. They have **not** been run
> against the hosted Supabase database — `0005` drops `chunks.embedding` and `0006`
> rebuilds the `tsv` column, so that remains your call.
>
> **Re-ingestion: required after migrating.** See the section below.

Scope of this phase:
1. **Remove Google Gemini entirely** — the platform is NVIDIA NIM only.
2. **Tier 0 accuracy fixes** — the five defects that are silently degrading retrieval today.

Out of scope for Phase A (tracked in `docs/AUDIT.md`, done in later phases): local
Postgres/storage, stdio MCP entrypoint, local embeddings, PaddleOCR-VL swap, ingestion
throughput.

> **⚠️ This phase requires re-ingesting the corpus.** Existing embeddings are invalid
> (see A-9 and B-2). Source PDFs in `ingestion_files/` are untouched; only derived data
> is rebuilt.

---

## Background: why retrieval is currently broken

Three independent defects compound into "semantic search silently does nothing":

| # | Defect | Effect |
|---|---|---|
| 1 | Frontend POSTs `llm_provider`; backend reads `provider` (`server.py:125`) | Provider selection never persists — every org is pinned to the `"gemini"` default |
| 2 | Gemini `text-embedding-004` returns **768**-d; `PDFSPECS_EMBEDDING_DIM` defaults to **1024** (`pgdb.py:26`) | Dimension mismatch on every chunk |
| 3 | `_clean_embedding` logs a warning and returns `None` on mismatch (`pgdb.py:141`) | Every vector is dropped; `chunks.embedding` is NULL corpus-wide |

`search_semantic` filters on `embedding IS NOT NULL`, so it returns zero rows. `hybrid_search`
degrades to lexical-only with no error surfaced to the API, the UI, or the MCP client.

Removing Gemini eliminates (1) and (2) structurally. (3) is fixed by failing loudly (B-1).

---

# Part A — Remove Gemini (NVIDIA NIM only)

Rationale: we're on NVIDIA free-tier keys. A second provider buys nothing and costs a
dual-dimension embedding problem, a dead settings path, and a whole branch of untested
code. One provider means one embedding dimension, which the schema already assumes.

**Canonical models after this change:**

| Role | Model | Notes |
|---|---|---|
| Embedding | `nvidia/nv-embedqa-e5-v5` | 1024-d — matches `vector(1024)` from migration 0003 |
| LLM (enrichment) | `meta/llama-3.1-8b-instruct` | already the NVIDIA default in `ingest.py:43` |

### A-1 · `src/enricher.py` — delete the Gemini code paths

**Remove:**
- `GEMINI_EMBEDDING_MODEL` constant (line 39)
- `_call_gemini_llm()` (93–139)
- `_call_gemini_embedding()` (191–206)
- `_call_gemini_synthesis()` (251–269)
- `_call_nvidia_synthesis()` (272–295) — dead, see below
- `generate_rag_response()` (298–344) — **dead code**, verified zero callers. It reads
  process-global `get_config()` for provider/keys, which the multi-tenant path abandoned.
- `default_embedding_model(provider)` (43–44) — collapses to a constant

**Change `ProviderSettings`** (47–59) — drop `provider` and `gemini_api_key`:

```python
@dataclass
class ProviderSettings:
    """Per-org NVIDIA NIM config for a single enrichment/embedding call (BYO key)."""
    nvidia_api_key: str = ""
    llm_model: str = NVIDIA_LLM_MODEL
    embedding_model: str = NVIDIA_EMBEDDING_MODEL
```

**Add** a module constant `NVIDIA_LLM_MODEL = "meta/llama-3.1-8b-instruct"` so the default
lives in exactly one place (today it's duplicated in `ingest.py:43` and `config.py:23`).

**Change `enrich_chunk()`** (347–427): drop the `if provider ==` dispatch; call the NVIDIA
functions directly. Raise `APIKeyMissingError` when `nvidia_api_key` is empty. The LLM-output
sanitizing block (377–404) is good and stays as-is.

### A-2 · `src/search.py` — NVIDIA-only query embedding

`_org_settings()` (21–28) and `_embed_query()` (31–47) lose their provider branch:

```python
def _org_settings(org_id: str) -> enricher.ProviderSettings:
    return enricher.ProviderSettings(
        nvidia_api_key=pgdb.get_setting(org_id, "nvidia_api_key", decrypt=True),
    )
```

`_embed_query` calls `_call_nvidia_embedding(..., input_type="query")` only. The
`input_type` asymmetry (`query` here vs `passage` at ingest) is correct and preserved —
it's what makes asymmetric retrieval work.

### A-3 · `src/ingest.py` — NVIDIA-only worker settings

`_org_provider_settings()` (39–53) drops the provider lookup and the `gemini-2.5-flash`
fallback; `llm_model` falls back to `enricher.NVIDIA_LLM_MODEL`.

### A-4 · `src/server.py` — `/api/v1/config`

`get_config_status()` (110–119) returns:

```python
{
    "provider": "nvidia",          # retained, constant, for frontend compatibility
    "has_nvidia_key": bool(...),
    "llm_model": ... or enricher.NVIDIA_LLM_MODEL,
    "embedding_model": enricher.NVIDIA_EMBEDDING_MODEL,
}
```

`save_config()` (122–133) accepts only `nvidia_api_key` (encrypted) and `llm_model`.
The `provider` write and the `gemini_api_key` loop are removed. Unknown keys are ignored
rather than silently half-applied.

### A-5 · `src/config.py` — defaults

- `SECRET_KEYS` → `{"nvidia_api_key"}`
- Drop `"provider"`, `"gemini_api_key"` defaults; `"llm_model"` → `"meta/llama-3.1-8b-instruct"`,
  `"embedding_model"` → `"nvidia/nv-embedqa-e5-v5"`
- Drop `self.provider` / `self.gemini_api_key` attributes and the `PDFSPECS_PROVIDER` /
  `GEMINI_API_KEY` env reads (119–121)

> Note: `config.py` still reads a legacy **SQLite** DB at `~/.pdfspecs/data.db` (line 81).
> That whole path is dead in the Postgres world. Phase A leaves it alone to keep the diff
> focused — it's removed in Phase B when local mode lands.

### A-6 · `config.json` — drop `provider` / `gemini_api_key`, update model names.

### A-7 · `requirements.txt` — drop `google-genai`

Unused even today (the Gemini calls used raw `requests`). Closes FIXPLAN 4.2.

### A-8 · `frontend/src/app/page.tsx` — `SettingsPanel` (592–681)

- Remove the provider radio group (632–650) and the `geminiKey` state + input.
- **Fix the field-name mismatch** that broke this panel: read `config.has_nvidia_key`
  (backend's name), not `config.nvidia_has_key`. POST `nvidia_api_key` only — the bogus
  `llm_provider` field goes away with the radio group.

This is the fix for the "Configured" badge that has never appeared.

### A-9 · Migration `0004_nvidia_only.sql`

Coerce existing org settings so no org is left pointing at a removed provider, and clear
embeddings that were produced by (or dropped because of) the Gemini path:

```sql
-- Gemini is removed platform-wide; NVIDIA nv-embedqa-e5-v5 (1024-d) is the only provider.
delete from org_settings where key = 'gemini_api_key';
update org_settings set value = 'nvidia' where key = 'provider';
-- Stale Gemini model names would be sent verbatim to the NIM API and 404.
delete from org_settings where key in ('llm_model', 'embedding_model')
  and value like 'gemini%' or value = 'text-embedding-004';
```

### A-10 · Documentation

`README.md` is substantially wrong (it documents `src/cli.py`, `src/mcp_server.py`,
`src/db.py`, `src/queue.py` — all deleted). Phase A does a **targeted** correction only:
remove Gemini from Features/Prerequisites/Configuration and fix the model names. The full
rewrite happens in Phase B when the local/stdio story is real, so the doc isn't rewritten
twice. `USAGE.md` and `.env.example` get their Gemini references stripped now.

---

# Part B — Tier 0 accuracy fixes

### B-1 · Fail loudly on embedding dimension mismatch

**File:** `src/pgdb.py:141-151`, `src/ingest.py`

Today a wrong-dimension vector is dropped with a `logger.warning` and ingestion reports
success — producing a corpus that looks fine and can't be searched semantically. That
silence is the actual bug; the dimension mismatch was just the trigger.

```python
class EmbeddingDimError(ValueError):
    """A vector's dimension doesn't match the platform's vector(N) column."""
```

`_clean_embedding` raises `EmbeddingDimError` instead of returning `None`. An empty/absent
embedding still returns `None` (a legitimate "provider call failed" state that degrades to
lexical). `process_task` lets the error propagate → the task lands in `failed` with a
message naming both dimensions, instead of quietly producing an unsearchable document.

### B-2 · Multi-vector embeddings — remove mean-pooling and the 8-window truncation

**Files:** `src/enricher.py:208-248`, `src/pgdb.py`, `src/ingest.py`, new migration

**The problem.** `_call_nvidia_embedding` splits a chunk into 1000-char windows, caps at
`MAX_WINDOWS = 8`, and **mean-pools** the window vectors into one:

- Content past 8000 chars is never embedded — it exists in the DB but is unreachable by
  semantic search.
- Averaging destroys the signal. A 2500-char chunk whose single relevant paragraph is one
  of 3 windows scores roughly 1/3 as similar as it should. The more content a chunk has,
  the *less* findable each part of it becomes — exactly backwards.

**The fix.** Store one row per window and score a chunk by its **best** window (max-sim)
instead of its average. This is standard multi-vector retrieval and it's the single largest
accuracy win in this phase.

New migration `0005_chunk_embeddings.sql`:

```sql
create table if not exists chunk_embeddings (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null references orgs(id) on delete cascade,
    chunk_id     uuid not null references chunks(id) on delete cascade,
    window_index int  not null,
    embedding    vector(1024) not null,
    unique (chunk_id, window_index)
);
create index if not exists idx_chunk_embeddings_org   on chunk_embeddings(org_id);
create index if not exists idx_chunk_embeddings_chunk on chunk_embeddings(chunk_id);
create index if not exists idx_chunk_embeddings_vec
    on chunk_embeddings using hnsw (embedding vector_cosine_ops);

-- Superseded by the table above. All existing vectors are invalid anyway (Gemini-era).
drop index if exists idx_chunks_embedding;
alter table chunks drop column if exists embedding;
```

`ON DELETE CASCADE` from `chunks` means `delete_document` keeps working unchanged.

**`enricher._call_nvidia_embedding`** returns `list[list[float]]` (one vector per window),
no pooling:
- `MAX_WINDOWS` removed. Windows are sent in batches of 32 per request (NIM input-list
  limits) and concatenated in order.
- `EMBED_WINDOW_CHARS` becomes env-configurable, **default lowered 1000 → 900**.
  `nv-embedqa-e5-v5` caps at 512 tokens; Devanagari is far denser per character than
  English, so 1000 chars of Sanskrit can exceed the cap and get silently truncated
  upstream. 900 with a **10% overlap between windows** so a sentence straddling a boundary
  survives in at least one window intact.

**`enrich_chunk`** returns `embeddings: list[list[float]]` (plural) instead of `embedding`.

**`pgdb.add_chunk`** takes `embeddings` and inserts the `chunk_embeddings` rows in the same
transaction as the chunk.

**`pgdb.search_semantic`** queries the new table, best-window-per-chunk:

```sql
SELECT chunk_id, max(score) AS score FROM (
    SELECT chunk_id, 1 - (embedding <=> %s) AS score
    FROM chunk_embeddings
    WHERE org_id = %s
    ORDER BY embedding <=> %s
    LIMIT %s
) w
GROUP BY chunk_id
ORDER BY score DESC
```

The inner `LIMIT` is over-fetched (windows collapse into fewer chunks) — it's set to
`limit * 4`.

### B-3 · Chunker: hard-split oversized paragraphs

**File:** `src/chunker.py:39-60`

A paragraph larger than `chunk_size` is currently emitted verbatim as its own chunk, with
no upper bound. OCR output routinely comes back as one blob per page with no `\n\n`, so
this is the common case, not the edge case — and it feeds straight into B-2's windowing.

Add `_split_oversized(text, chunk_size, chunk_overlap)`, splitting on sentence boundaries
with a character-split fallback, carrying `chunk_overlap` between the pieces.

**Sentence terminators must include the Devanagari danda** — `।` (U+0964) and double danda
`॥` (U+0965) — alongside `.`/`?`/`!`. Splitting Sanskrit slokas on ASCII punctuation alone
would find no boundaries at all and degrade to blind character splitting mid-word.

### B-4 · Lexical search: stop losing English matches to the `simple` dictionary

**Files:** new migration `0006`, `src/pgdb.py:209-227`

`to_tsvector('simple', ...)` does no stemming and no stopword removal, so `"treatment of
fevers"` does not match a passage containing `"fever"`. `simple` is the right call for
Devanagari (which the `english` dictionary would mangle), so the fix is to index **both**
and OR them at query time — not to switch.

Migration `0006_tsv_english_simple.sql` rebuilds the generated column as a union:

```sql
drop index if exists idx_chunks_tsv;
alter table chunks drop column if exists tsv;
alter table chunks add column tsv tsvector generated always as (
    to_tsvector('english', coalesce(text_content,'') || ' ' || coalesce(summary,''))
    || to_tsvector('simple',  coalesce(text_content,'') || ' ' || coalesce(summary,''))
) stored;
create index idx_chunks_tsv on chunks using gin(tsv);
```

`tsvector || tsvector` concatenates, so the column holds stemmed English tokens *and* raw
tokens. Query side uses `||` on **tsquery**, which is OR:

```sql
websearch_to_tsquery('english', %s) || websearch_to_tsquery('simple', %s)
```

An English query now matches via the stemmed side; a Devanagari or transliterated query
matches via the raw side. Cost is roughly a doubled GIN index — acceptable at this corpus
size.

### B-5 · OCR: fix the silent-failure path

**File:** `src/parser.py:23-42, 121-168`

```python
pix = page.get_pixmap(dpi=300)
img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
```

When the pixmap carries an alpha channel (`pix.n == 4`), `frombytes("RGB", ...)` does
**not** raise — verified locally, it reads 4-byte RGBA samples as 3-byte RGB (2,003,960
bytes supplied where 1,502,970 were expected), shifting every pixel and handing tesseract
a scrambled image. Garbage OCR, no exception, no log.

Where it *does* raise, the bare `except: return None` falls back to the text layer — and
on a scanned page that's empty **by definition**, so the page is ingested as empty text
and nothing anywhere reports a problem.

Three changes:

1. **Decode correctly** — `Image.open(io.BytesIO(pix.tobytes("png")))`, which handles
   colorspace and alpha itself, instead of hand-assembling from `pix.samples`.
2. **Stop swallowing errors** — `except Exception` logs at WARNING with the page number
   before returning `None`. A failed OCR must be visible in the worker log.
3. **Check the language packs once at startup** — `pytesseract.get_languages()` vs the
   requested `eng+hin+san`, logging any that are missing. A missing `san.traineddata`
   currently manifests as "every scanned page is blank" with no other clue.
   *(Verified present locally: this Mac has all 163 packs incl. `hin`, `san`, `mar`, `nep`.)*

Also add **per-page error isolation** in `parse_pdf` (151–159): one unparseable page
currently aborts the entire document. Log it, record the page as empty, continue.

### B-6 · RRF candidate pool (bundled micro-fix)

**File:** `src/search.py:55`

`pool = max(limit * 2, limit)` fuses two top-10 lists at the default `limit=5`. Reciprocal
Rank Fusion needs a deep candidate pool to do anything useful — a document ranked #12
lexically and #11 semantically (a strong hybrid signal) is invisible today.

`pool = max(50, limit * 10)`.

Strictly this was Tier 3 in the audit, not Tier 0. It's one line, it's purely an accuracy
change, and it makes B-2's improved semantic ranking actually reachable — so it rides along
here rather than waiting.

---

## Migration order

Applied in sequence against the existing database:

| File | Purpose |
|---|---|
| `0004_nvidia_only.sql` | Coerce `org_settings` off Gemini |
| `0005_chunk_embeddings.sql` | Multi-vector table; drop `chunks.embedding` |
| `0006_tsv_english_simple.sql` | Rebuild `tsv` as english ∪ simple |

There is still **no migration runner** — these are applied by hand. A runner is Phase B.

---

## Re-ingestion

Required. After migrating:

1. Delete existing documents (chunks/entities/keywords cascade).
2. Confirm the org's NVIDIA key is saved in Settings.
3. Re-upload from `ingestion_files/`.

Every existing vector is invalid regardless — they were either dropped at write time (the
768-vs-1024 bug) or produced by a model we no longer use. Nothing is lost that wasn't
already unusable.

---

## Verification

Each item has a concrete check, run against a re-ingested corpus:

| Change | How we verify it |
|---|---|
| A (Gemini removal) | `grep -ri gemini src/ frontend/src/` returns nothing; ingest + search complete end-to-end on an NVIDIA key |
| A-8 (settings panel) | Saving an NVIDIA key persists; the "Configured" badge appears on reload |
| B-1 | Unit test: a 768-d vector raises `EmbeddingDimError`; the task lands `failed` with both dimensions in the message |
| B-2 | `select count(*) from chunk_embeddings` > `count(*) from chunks` for a multi-window corpus; a query matching only the *tail* of a long chunk now retrieves it (it cannot today) |
| B-3 | Unit test: a 10k-char paragraph with no `\n\n` yields multiple chunks, each ≤ `chunk_size`; a Sanskrit sloka block splits on `।` |
| B-4 | `"fevers"` retrieves a passage containing only `"fever"`; a Devanagari term still matches exactly |
| B-5 | Unit test: an RGBA pixmap page OCRs instead of returning `None`; a forced OCR failure emits a WARNING |
| B-6 | A known hybrid-signal document (mid-rank in both lists) appears in the top 5 |

Test coverage is currently ~0 (only `tests/test_chunker.py` survives; `test_db.py` and
`test_server.py` were deleted). Phase A adds tests for `chunker`, `parser`, and the pgdb
embedding guard — the three places with pure, DB-free logic worth pinning down now.

---

## Changed files

```
src/enricher.py         Gemini paths deleted; multi-vector embeddings; NVIDIA_LLM_MODEL
src/search.py           NVIDIA-only query embedding; RRF pool 50
src/ingest.py           NVIDIA-only settings; embeddings (plural) through to add_chunk
src/server.py           /api/v1/config NVIDIA-only
src/pgdb.py             EmbeddingDimError; chunk_embeddings write + max-sim search; tsquery OR
src/parser.py           OCR decode fix; logged failures; lang check; per-page isolation
src/chunker.py          _split_oversized with Devanagari danda support
src/config.py           NVIDIA-only defaults
config.json             NVIDIA-only
requirements.txt        drop google-genai
frontend/.../page.tsx   SettingsPanel: NVIDIA-only, field-name fix
db/migrations/0004_nvidia_only.sql          new
db/migrations/0005_chunk_embeddings.sql     new
db/migrations/0006_tsv_english_simple.sql   new
tests/test_chunker.py   oversized-paragraph + danda cases
tests/test_parser.py    new — OCR decode + fallback
tests/test_pgdb.py      new — embedding dimension guard
README.md, USAGE.md, .env.example           Gemini references removed
```

---

# Phase B — Local-first

> ## Status: complete and verified
>
> Local Postgres + pgvector, local file storage, single-user auth, a CLI, a stdio MCP
> entrypoint, and a migration runner. Verified end to end on this machine: migrations
> applied, org bootstrapped, a 172-page corpus PDF parsed, stdio MCP handshake completed
> with all four tools listed. 17 unit tests pass.

The goal: run the whole system with no Supabase account, no Render, and no network
dependency beyond the NVIDIA API. Before this, `pgdb` raised without `DATABASE_URL`,
`storage` required `SUPABASE_URL` + `SUPABASE_SECRET_KEY`, and `auth` demanded
`SUPABASE_JWKS_URL` on every admin call — nothing ran offline.

## B-1 · Local Postgres with pgvector

Homebrew's `pgvector` bottle is built against Postgres 17/18, but this machine runs
Postgres 14 with four unrelated databases on it. Rather than migrate that cluster, pgvector
0.8.6 was compiled from source against PG14 and a dedicated `pdfspecs` database created.

```bash
git clone --branch v0.8.6 --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector
make        PG_CONFIG=/usr/local/opt/postgresql@14/bin/pg_config PG_SYSROOT="$(xcrun --show-sdk-path)"
make install PG_CONFIG=/usr/local/opt/postgresql@14/bin/pg_config PG_SYSROOT="$(xcrun --show-sdk-path)"
createdb pdfspecs
psql -d pdfspecs -c 'create extension if not exists vector;'
```

`PG_SYSROOT` is required: the PG14 build config references a `MacOSX14.sdk` that no longer
exists on this machine, so the build fails with `'stdio.h' file not found` without it.

Nothing else about the existing cluster changed — the extension only adds files under the
PG14 install prefix.

## B-2 · Migration runner — `src/migrate.py`

Migrations were previously pasted by hand into the Supabase SQL editor, leaving no record
of what had been applied where. That's untenable with two databases (local and hosted).

- Applies `db/migrations/*.sql` in filename order, each in its own transaction.
- Records every applied file in a `schema_migrations` table, so re-running is a no-op.
- Skips files marked `@supabase-only` on a database with no `auth` schema — currently
  `0002_rls_policies.sql`, whose policies all reference `auth.uid()`.

```bash
python -m src.migrate --status   # list applied / pending / skipped
python -m src.migrate            # apply pending
```

## B-3 · Pluggable storage — `src/storage.py`

`PDFSPECS_STORAGE_BACKEND` selects `local` or `supabase` behind the existing interface
(`upload_bytes` / `download_to_path` / `delete_object`), so no caller changed. Local files
live under `PDFSPECS_DATA_DIR/uploads/<org_id>/<uuid>-<name>`, keeping the same org-prefixed
key layout as the bucket.

Unset, the backend defaults to `supabase` when `SUPABASE_URL` is configured and `local`
otherwise — so an existing hosted `.env` keeps working untouched.

Storage keys are resolved and re-checked for containment under the uploads root, so a
malformed key can't escape the directory.

## B-4 · Single-user local auth — `src/auth.py`

With `PDFSPECS_LOCAL_MODE=1`, `authenticate_admin` ignores the token entirely and returns
one implicit org owned by a fixed sentinel uid (`00000000-…-0001`; `orgs.owner_auth_id` is
a uuid column, so it must be a valid UUID). `require_admin` needs no change — it already
passes the (absent) token straight through.

**This removes authentication from the admin API**, which is why the local template binds
to `127.0.0.1`. Org API keys are unaffected and still gate the HTTP MCP surface.

## B-5 · stdio MCP entrypoint — `src/mcp_server.py`

The transport local MCP clients actually use: a subprocess speaking JSON-RPC over
stdin/stdout, needing no port, no hosting, and no API key round-trip.

```bash
claude mcp add openpdfspecs -- /abs/path/.venv/bin/python -m src.mcp_server
```

Two details that matter:

- **Logging goes to stderr.** stdout carries the JSON-RPC frames; a single stray byte
  there corrupts the stream and drops the client connection.
- **`.env` is loaded before importing anything env-dependent** (`pgdb.EMBEDDING_DIM`,
  `storage.backend()` read the environment at import time). An MCP client launches the
  process without a shell profile, so the `.env` is the only source for these.

### `src/mcp_tools.py` — shared tool definitions

The four tools were defined inline in `server.py`. With a second transport they'd have
been duplicated — the exact drift that FIXPLAN 4.1 called out for search. They now live in
one module, parameterised by an `org_resolver`: the HTTP transport resolves per request
from the API key, stdio resolves once at startup. `search_knowledge`'s `limit` is also
clamped to 50 here, since it arrives straight from a model.

## B-6 · Local CLI — `src/cli.py`

The dashboard authenticates through Supabase, so a local install had no way to ingest a
PDF at all. The CLI is the local counterpart:

```bash
python -m src.cli init                  # create/upgrade schema
python -m src.cli set-key nvapi-...     # store the NVIDIA key (encrypted)
python -m src.cli ingest FILE.pdf       # queue
python -m src.cli worker                # process the queue
python -m src.cli ls | status | info
python -m src.cli search "jwara"
```

`info` prints the fully resolved configuration (org, database, storage backend, key
status, models, migration state) — the first thing to check when something's off.

## B-7 · `config.py` — SQLite path removed

`Config.load()` opened a SQLite database at `~/.pdfspecs/data.db` to read settings, left
over from before the Postgres migration. Nothing had written to it in a long time, so it
was a second source of truth that could only ever be stale. Removed, along with the
now-unused `db_path` handling in `save()`. `Config` is now purely platform settings
(chunking, OCR policy, upload limits); per-org values come from `org_settings` via `pgdb`.

Added: `load_dotenv_if_present()`, a ~10-line `.env` reader (no interpolation, no export
syntax, no new dependency) used by the CLI and stdio server.

## B-8 · Frontend local mode

`NEXT_PUBLIC_LOCAL_MODE=1` synthesizes a session so the dashboard renders without a
Supabase login; `apiFetch` already omits the Authorization header when there's no session,
which is exactly what the local backend expects. The header shows "Local mode" instead of
a sign-out button.

## Files

```
src/migrate.py       new — migration runner
src/mcp_server.py    new — stdio MCP entrypoint
src/mcp_tools.py     new — shared tool definitions (both transports)
src/cli.py           new — local CLI
src/storage.py       pluggable local/supabase backend
src/auth.py          local_mode(), local_org_id(), local bypass in authenticate_admin
src/config.py        SQLite path removed; load_dotenv_if_present() added
src/server.py        MCP tools sourced from mcp_tools
db/migrations/0002_rls_policies.sql   marked @supabase-only
.env.local.example   new — local-only environment template
frontend/src/app/page.tsx             NEXT_PUBLIC_LOCAL_MODE bypass
```

## Fixes made to in-flight Phase C/D work

Phase C (local BGE-M3 embeddings) and Phase D (OCR provider interface) landed in the
working tree during this phase. Three defects in that code were blocking:

1. **Ingestion was completely broken.** `parse_pdf` constructed a PaddleOCR provider
   eagerly on every call, and `PaddleOCR(lang='devanagari')` raises on paddleocr 3.7.0 —
   so *every* PDF failed before a single page was read, text-layer documents included.
   The provider is now built lazily, on the first page that actually needs OCR, and cached
   for the rest of the document.
2. **`get_ocr_provider` raised instead of degrading.** It now falls back to Tesseract with
   an ERROR log. Losing OCR quality is recoverable; losing the parse is not.
3. **`extract_page_text_and_tables(page, tables, ...)` discarded its `tables` argument**,
   overwriting it with `page.find_tables()` — so tables were detected twice per page and
   the caller's value was dead. It now only detects when not supplied.

Also: `paddleocr` was imported but missing from `requirements.txt`; added.

The test suite dropped from 72s to 0.9s as a direct result of (1) — it had been loading an
OCR model on every parse.

---

# Phase E — Two-stage ingestion

## The problem

Ingestion was a single pass: parse → chunk → **enrich (one LLM call per chunk)** → store.
Nothing was written until every chunk had been enriched.

Measured on this corpus (28 PDFs, 5,384 pages, ~30.5M characters ≈ 13,500–15,700 chunks),
at the default 1.5s throttle with concurrency 1, that is **~5.6 hours before a single
passage becomes searchable** — and any failure mid-document discarded the whole thing.

The key observation: since Phase C moved embeddings local, the search-critical path —
parse, chunk, embed, store — makes **no network calls at all**. It was being held hostage
by an LLM call that only produces a nice-to-have summary.

## The split

| Stage | Work | Network | Result |
|---|---|---|---|
| **1 — store** (`process_task`) | parse → chunk → embed → bulk insert | none with the `local` embedding backend; batched embedding calls with `nvidia` | Document fully searchable, lexical + semantic |
| **2 — enrich** (`run_enrichment_pass`) | LLM summary + entities per chunk | throttled NVIDIA calls | Summaries appear; `entity_lookup` populated |

`chunks.enriched_at` (migration 0007) is the marker: NULL means stage 2 still owes this
chunk. A partial index on `(org_id) WHERE enriched_at IS NULL` keeps the pending query
cheap and costs nothing once a corpus is fully enriched.

Stage 2 is **resumable by construction** — each chunk is marked as it completes, so an
interrupted run picks up where it stopped. A chunk whose LLM call keeps failing is marked
done with no summary rather than wedging the queue; it stays searchable via its text and
embeddings regardless.

```bash
python -m src.cli worker            # stage 1, then backfills stage 2 while idle
python -m src.cli worker --no-enrich # stage 1 only
python -m src.cli enrich             # stage 2 explicitly; -n to cap
python -m src.cli info               # shows "enrichment 412/502 done — 90 pending"
```

The idle worker enriches in slices of 25 so a newly queued document is picked up promptly
instead of waiting behind a whole corpus.

## Dropping `chunk_keywords`

`pgdb.add_keyword` INSERTed into this table and **nothing has ever read from it** — no
SELECT, no JOIN, no tool, no endpoint. Every keyword cost a third of the enrichment
prompt's output plus an INSERT round-trip, then sat unread. Removed from the prompt, the
data layer, and the schema (migration 0007).

Entity lookup — the feature one might assume this powered — goes through
`entities`/`chunk_entities`, untouched.

## Other work in this phase

**Bulk chunk insert** (`pgdb.add_chunks_bulk`). `add_chunk` took a pooled connection per
chunk *and* per embedding window — roughly 35,000 round-trips for a full corpus. Now the
whole document is one transaction, so a crash also can't leave half a document behind.

**Batched embedding** (`enricher.embed_texts_batch`). Windows across many chunks are
flattened into one `encode()` call so sentence-transformers fills its batches; embedding
per chunk meant hundreds of tiny, under-filled forward passes per document.

**Apple Silicon memory fixes.** The first full run died part-way through with
`RuntimeError: MPS backend out of memory` after ~8 minutes of successful work. Three
causes, all addressed:

- BGE-M3 defaults to `max_seq_length=8192`; our windows are ~900 chars (~300 tokens), so
  the headroom bought nothing and cost a great deal of memory. Capped via
  `PDFSPECS_EMBED_MAX_SEQ_LEN` (default 512).
- Batch size 32 was too aggressive for a 568M-param model on shared system memory;
  default lowered to 8 (`PDFSPECS_EMBED_BATCH_SIZE`).
- MPS retained and fragmented freed blocks across batches; `torch.mps.empty_cache()` now
  runs between batches.

`PDFSPECS_EMBED_DEVICE` can force `cpu`/`mps`/`cuda` if the automatic choice misbehaves.

## Files

```
db/migrations/0007_two_stage_ingestion.sql  new — enriched_at, partial index, drop chunk_keywords
src/ingest.py     process_task = stage 1 only; run_enrichment_pass = stage 2; worker backfills when idle
src/enricher.py   extract_metadata (LLM only) + embed_texts_batch; enrich_chunk removed;
                  keywords dropped from the prompt; MPS memory fixes
src/pgdb.py       add_chunks_bulk, chunks_pending_enrichment, count_pending_enrichment,
                  orgs_with_pending_enrichment, save_enrichment, mark_enrichment_failed;
                  add_keyword removed
src/cli.py        `enrich` command; `worker --no-enrich`; enrichment status in `info`
```

## Pluggable embedding backend

Benchmarking stage 1 turned up a hardware fact that had been assumed away: **this host is
an Intel Mac** (Core i7-8750H, 2018, 6-core, 16 GB) — not Apple Silicon. Two consequences:

- "MPS" here is a weak integrated GPU, so it is *slower* than the CPU. Measured on 40 real
  chunks: **8.3 chunks/min on MPS vs 19.8 on CPU.**
- BGE-M3 is a 568M-param XLM-RoBERTa-large. At 19.8 chunks/min, the ~15,700-chunk corpus
  needs **~13 hours** — worse than the 5.6h single-pass flow the split replaced.

The NVIDIA embeddings endpoint accepts 32 windows per request, so the same corpus
(~37,700 windows → ~1,180 requests at the 1.5s throttle) takes **~30 minutes**: roughly
26× faster despite being rate-limited. Batching, not locality, dominates here.

Both models are 1024-d, so the backend is selectable with **no migration**:

```
PDFSPECS_EMBEDDING_BACKEND=local    # BAAI/bge-m3, offline, no quota   (default)
PDFSPECS_EMBEDDING_BACKEND=nvidia   # nv-embedqa-e5-v5, ~26x faster here
```

`local` remains the default — offline operation and zero API quota are the reasons Phase C
chose it, and both still hold. The switch simply makes the trade-off explicit and
reversible per environment rather than baked into the code.

`embed_texts_batch()` is the single embedding entrypoint for both backends and both call
sites (ingest and query); `_call_local_embedding` was removed so the two paths can't
diverge.

### Local-backend tuning (in `.env.local.example`)

```
PDFSPECS_EMBED_DEVICE=cpu        # MPS is slower on an Intel Mac; torch picks it by default
PDFSPECS_EMBED_BATCH_SIZE=16
PDFSPECS_EMBED_MAX_SEQ_LEN=512   # BGE-M3 defaults to 8192; our windows are ~900 chars
```

### Correction to `docs/AUDIT.md`

The original OCR recommendation cited PaddleOCR-VL's "official Apple Silicon path via
MLX-VLM with Metal acceleration" as a major advantage. **MLX is Apple-Silicon-only**, so
that argument never applied to this Intel host. The audit has been corrected. It is moot
in practice — 1 page in 5,384 needs OCR — but the reasoning was wrong on its own terms.
