# OpenPDFSpecs — Remediation Plan

Ordered fix plan from the code audit. Work top-to-bottom; phases are grouped so related
changes land together. Check items off as we go. **Delete this file once all items are done.**

Legend: 🔴 bug · 🟠 security · 🟡 dead config · 🟢 perf/arch · ⚪ cleanup

---

## Phase 0 — Groundwork (do first)

- [x] **0.1 `git init`** — done (branch `main`). Not committed yet, per request.
- [x] **0.2 Remove stray empty `openpdfspecs.db`** — done (was 0 bytes; real DB is `~/.pdfspecs/data.db`). ⚪
- [x] **0.3 Enable WAL + busy_timeout** in `db.get_connection()` (`src/db.py:6`) — done.
      Added `PRAGMA journal_mode=WAL;` + `PRAGMA busy_timeout=5000;`.
      *Verified:* all 10 tests pass; `PRAGMA journal_mode` returns `wal`, `busy_timeout` returns `5000`.

---

## Phase 1 — Correctness bugs (highest impact)

- [x] **1.1 🔴 Fix OCR wiring** — `src/parser.py` — done.
      - Replaced buggy `getattr(config, 'enable_ocr', True)` with real use of
        `config.ocr_mode` (`auto`|`always`|`never`) + `config.ocr_min_chars`.
      - Split extraction into `_ocr_page` / `_extract_text_layer` / `_tables_to_markdown`
        helpers; `extract_page_text_and_tables` now decides per page (auto probes the
        text-layer length cheaply before OCR'ing). OCR falls back to the text layer on
        failure/empty.
      *Verified:* on a text page — `auto` skips OCR (~60ms, identical to `never`) vs the
      old always-on path (~2860ms); forcing the auto threshold high triggers a real OCR
      pass. All 10 tests pass.

- [x] **1.2 🔴 Embedding dimension/model tracking** — done.
      - Added `embedding_model TEXT` column to `chunks` (in CREATE + `ALTER TABLE`
        auto-migration for existing DBs).
      - `enrich_chunk` now returns `embedding_model`; `queue`→`add_chunk` persists it.
      - `get_all_chunks_with_embeddings(expected_dim=...)` filters to the query vector's
        dimension and logs a warning naming how many chunks were excluded. Both search
        call sites (`mcp_server`, `cli`) pass `expected_dim=len(q_vec)`.
      *Verified:* mixed 768-d/1024-d corpus → only matching dim returned, rectangular
      matrix (no ragged-array crash), warning fired; legacy DB (no column) auto-migrates;
      all 10 tests pass.

- [ ] **1.3 🔴 Harden FTS `MATCH`** — `src/db.py:242`
      - Sanitize/quote the user query before `chunks_fts MATCH ?` (e.g. wrap tokens in
        double quotes) so FTS5 operators/punctuation don't error out into the LIKE fallback.
      *Verify:* search for a query containing `"`, `:`, `-`, `AND` — no OperationalError,
      results still returned.

- [ ] **1.4 🔴 Fix FTS dedupe on re-insert** — `src/db.py:187`
      - `chunks_fts` has no unique key, so `INSERT OR REPLACE` duplicates rows. Either
        `DELETE FROM chunks_fts WHERE chunk_id = ?` before insert, or switch to an
        external-content FTS table keyed to `chunks`.
      *Verify:* insert same chunk_id twice → exactly one FTS row.

---

## Phase 2 — Security

- [ ] **2.1 🟠 Bind to loopback** — `src/server.py:250`
      Change `host="0.0.0.0"` → `host="127.0.0.1"` (make it configurable via env if remote
      access is ever genuinely needed).

- [ ] **2.2 🟠 Close the first-token bootstrap hole** — `src/server.py:33,142`
      Anyone on the network can currently mint the first token before auth turns on.
      Options (pick one): require a local setup secret to create the first token, or only
      allow token creation from loopback, or ship auth-on-by-default with a generated
      bootstrap token printed to the console on first run.

- [ ] **2.3 🟠 Hash agent tokens at rest** — `src/db.py:293,310`
      Store a hash of `sk-pdfspecs-...` tokens; compare in constant time in `verify_api_key`.

- [ ] **2.4 🟠 Tighten CORS** — `src/server.py:62`
      Replace `allow_origins=["*"]` with the concrete frontend origin (e.g.
      `http://localhost:3000`), configurable via env.

---

## Phase 3 — Performance & dead config

- [ ] **3.1 🟡→🟢 Parallelize enrichment (wire `enrich_concurrency`)** — `src/queue.py:170`
      Replace the sequential per-chunk loop with a `ThreadPoolExecutor(max_workers=
      config.enrich_concurrency)`. Keep DB writes ordered/thread-safe (each thread enriches;
      main thread writes, or use short-lived connections). Depends on **0.3 (WAL)**.
      *Verify:* ingestion wall-clock drops roughly linearly with concurrency.

- [ ] **3.2 🟢 Cache embedding matrix for search** — `src/db.py:204`, `src/mcp_server.py:61`
      Stop re-reading + re-deserializing all embeddings on every query. Cache the NumPy
      matrix in memory, invalidate on ingest/delete. (Stretch: evaluate `sqlite-vec`/`faiss`.)
      *Verify:* repeated searches don't re-hit the full table (add a debug log/counter).

- [ ] **3.3 🟢 Proper Reciprocal Rank Fusion** — `src/mcp_server.py:84`, `src/cli.py:176`
      Use `1/(k + rank)` for BOTH lexical and semantic ranked lists (use the BM25 `rank`
      you already fetch). Removes the current cosine-vs-reciprocal-rank scale mismatch.

- [ ] **3.4 🟡 Enforce `max_upload_mb`** — `src/server.py:172`
      Reject uploads larger than `config.max_upload_mb` (check size while streaming to disk;
      clean up partial file on rejection). Return 413.

- [ ] **3.5 🟡 Wire `max_task_attempts`** — `src/queue.py`
      On failure, if `attempts >= config.max_task_attempts` leave as `failed`; otherwise
      requeue as `pending` for retry. Make the attempts machinery actually do something.

---

## Phase 4 — Refactor & cleanup

- [ ] **4.1 🟢 De-duplicate search logic** — `src/cli.py:117-208` vs `src/mcp_server.py:40-111`
      Extract a single `search()` (new `src/search.py` or into `db.py`); call it from CLI,
      MCP server, and the REST `/api/v1/search` endpoint. Do this AFTER 3.2/3.3 so the
      improved logic is written once.

- [ ] **4.2 ⚪ Drop unused `google-genai` dependency** — `requirements.txt` (Gemini uses raw `requests`).
- [ ] **4.3 ⚪ Unify default model name** — config.json / `config.py` / `cli.py:97` / README all agree (`gemini-2.5-flash` vs `gemini-1.5-flash`).
- [ ] **4.4 ⚪ Clean logging hack** — `src/mcp_server.py:14` → plain `import sys` at top.
- [ ] **4.5 ⚪ Migrate to FastAPI `lifespan`** — `src/server.py:71` (replace deprecated `on_event`).
- [ ] **4.6 ⚪ Remove double `reset_stuck_tasks()`** — `src/server.py` startup vs `src/queue.py` loop.
- [ ] **4.7 ⚪ Portable test DB path** — `tests/test_server.py:7` use `tempfile`/`tmp_path`.
- [ ] **4.8 ⚪ Use `Path.expanduser()`** for db_path — `src/config.py:68`.

---

## Suggested working order (dependencies honored)
`0.1 → 0.2 → 0.3 → 1.1 → 1.2 → 1.3 → 1.4 → 2.1 → 2.2 → 2.3 → 2.4 → 3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 4.*`

Fastest path to "meaningfully better": **0.3, 1.1, 2.1, 2.2, 3.1** (biggest correctness,
security, and speed wins with small diffs).
