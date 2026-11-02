# OpenPDFSpecs — Code Audit

Full-codebase audit preceding the revamp. Findings are tiered by impact. The remediation
plan for Tier 0 lives in `docs/REVAMP.md`; later tiers are scheduled after it.

Supersedes `docs/FIXPLAN.md`, which describes the pre-Postgres SQLite codebase and is now
largely historical.

---

## Orientation: docs vs. reality

`README.md` documents `src/cli.py`, `src/mcp_server.py`, `src/db.py`, and `src/queue.py`.
**All four are deleted** — only stale `.pyc` files remain in `src/__pycache__/`. The repo
migrated to hosted multi-tenant Postgres and the docs were never updated.

Practical consequence: **there is no stdio MCP entrypoint**. The only MCP surface is HTTP
at `/mcp` (`server.py:369`), behind org API keys and Supabase.

---

## Tier 0 — silently degrading retrieval accuracy

### 0.1 Semantic search is dead on the default configuration
Three defects compound:

1. The frontend POSTs `llm_provider` (`page.tsx:605`); the backend reads `provider`
   (`server.py:125`). The value is discarded — **provider selection has never persisted**,
   so every org stays on the `"gemini"` default.
2. Gemini `text-embedding-004` is 768-d; `PDFSPECS_EMBEDDING_DIM` defaults to 1024
   (`pgdb.py:26`) and is unset in `.env`.
3. `_clean_embedding` (`pgdb.py:141`) drops any mismatched vector with a `logger.warning`
   and returns `None`.

Result: `chunks.embedding` is NULL corpus-wide, `search_semantic` filters those out, and
`hybrid_search` silently degrades to lexical-only. No error reaches the API, UI, or MCP
client. The same read-path mismatch (`config.gemini_has_key` vs `has_gemini_key`) is why
the Settings "Configured" badge never appears.

### 0.2 Gemini embeddings never set `task_type`
`enricher.py:191` posts a bare `{"content": {...}}`. Gemini supports `RETRIEVAL_DOCUMENT`
vs `RETRIEVAL_QUERY`; asymmetric retrieval is exactly what a RAG corpus needs. The NVIDIA
path does this correctly via `input_type`. *(Moot once Gemini is removed.)*

### 0.3 Lexical search uses the `simple` dictionary
`to_tsvector('simple', ...)` (`0001_init_schema.sql:57`) does no stemming and no stopword
removal — `"treatment of fevers"` will not match `"fever"`. Correct for Devanagari,
badly wrong for the English half of the corpus, which is how users query.

### 0.4 OCR silently scrambles any page with an alpha channel
`Image.frombytes("RGB", [pix.width, pix.height], pix.samples)` (`parser.py:31`) does **not
raise** when `pix.n == 4` — verified locally: it reads 4-byte RGBA samples as 3-byte RGB
(2,003,960 bytes supplied where 1,502,970 were expected), shifting every pixel and handing
tesseract a scrambled image. No exception, no log line, just garbage OCR output.

Where it *does* raise, the bare `except: return None` (`parser.py:33`) falls back to the
text layer — which on a scanned page is empty by definition, so the page is ingested as
empty text with nothing reported. A missing tesseract language pack produces the identical
silent failure.

### 0.5 NVIDIA embeddings truncate at 8000 chars, then average away the signal
`enricher.py:219-245`: 1000-char windows, `MAX_WINDOWS = 8`, then mean-pooled into one
vector. Content past window 8 is never embedded. Mean-pooling dilutes any single matching
window — a chunk whose one relevant paragraph is 1 of 3 windows scores ~1/3 as similar as
it should. More content makes each part *less* findable.

### 0.6 The chunker never splits an oversized paragraph
`chunker.py:39-60` emits a paragraph larger than `chunk_size` verbatim, unbounded. OCR
output routinely arrives as one blob per page with no `\n\n`, so this is the common case —
and it feeds directly into 0.5.

---

## Tier 1 — blockers for local-first operation

Every module hard-depends on Supabase cloud:

| Module | Requirement |
|---|---|
| `pgdb.py:39` | `DATABASE_URL` — raises without it |
| `storage.py:34,41` | `SUPABASE_URL` + `SUPABASE_SECRET_KEY` |
| `auth.py:45` | `SUPABASE_JWKS_URL` on every admin call |

No local storage backend, no single-user auth mode, no stdio MCP server.

`config.py:81` still opens a **SQLite** DB at `~/.pdfspecs/data.db` to read settings — a
dead pre-migration path now coexisting with the `org_settings` table. Half of `Config`'s
fields (`provider`, api keys, `llm_model`) are ignored by the multi-tenant code; the other
half (`chunk_size`, `ocr_mode`, `max_upload_mb`) are still live globals. Two sources of
truth.

`db/migrations/` has **no runner** — SQL is applied by hand.

---

## Tier 2 — ingestion throughput

1. **The global throttle holds a mutex across `time.sleep`** (`enricher.py:30-34`) at 1.5s
   between all provider calls, with `PDFSPECS_ENRICH_CONCURRENCY` defaulting to 1
   (`ingest.py:32`). Two calls per chunk. A 500-page book ≈ 1500 chunks ≈ 3000 calls ≈
   **75 minutes of pure waiting**.
2. **One LLM call per chunk** for summary + keywords + entities (`ingest.py:68`) — the
   dominant cost, and the best candidate for batching or lazy evaluation.
3. **No embedding batching** on the Gemini path (one text per request). Gemini has
   `batchEmbedContents`; NVIDIA already accepts a list.
4. `enrich_concurrency` and `max_task_attempts` exist in `Config` but `ingest.py` reads
   them from env only, ignoring the config object.

---

## Tier 3 — correctness, security, hygiene

- **Uploads are fully buffered before the size check** — `server.py:155-159` does
  `await file.read()` then compares to `max_upload_mb`. A large POST OOMs the process
  before it can return 413.
- **`reset_stuck_tasks()` is global and runs twice** — `server.py:45` and again in
  `ingest.py:185`. It deletes partial documents across *all* orgs. (FIXPLAN 4.6 flagged
  this; still present.)
- **HNSW index doesn't cover `org_id`** (`0001:69`). `WHERE org_id = %s ORDER BY embedding
  <=> %s LIMIT n` post-filters HNSW output, so with several orgs you can get fewer than
  `limit` rows or fall off the index. Moot with one org; real at scale.
- **Fernet decrypt failure returns `""`** (`crypto_utils.py:61`), indistinguishable from
  "no key configured". A wrong `PDFSPECS_FERNET_KEY` surfaces as "API key is missing".
- **RRF candidate pool is far too small** — `pool = max(limit * 2, limit)` (`search.py:55`)
  fuses two top-10 lists. RRF needs 50–100 per list.
- **`limit` is unclamped** in `search_knowledge` (`server.py:289`) — an agent can pass
  `limit=100000`.
- **Tenant binding via contextvar is fragile.** `server.py:222` sets `_current_org` and
  resets it in a `finally` that may run before the MCP tool executes. It is currently safe
  *only* because anyio copies the context at task spawn. An `mcp` version bump could turn
  this into a silent cross-tenant leak. Needs an explicit test, or pass org through
  explicitly.
- **ISSUE-1 still open** — uploads accepted with no provider key configured; the task burns
  all 3 retries and lands in `failed`.
- **Dead code**: `generate_rag_response`, `_call_gemini_synthesis`, `_call_nvidia_synthesis`
  (`enricher.py:251-344`) have zero callers and read process-global config.
- **Test coverage ~0** — only `tests/test_chunker.py` survives; `test_db.py` and
  `test_server.py` are deleted (pycache remains). Nothing covers search, pgdb, parser,
  ingest, or auth.
- **Stray files**: `temp.tsx` (untracked, 20 KB), `test_data.db-shm/-wal`,
  `test_server_data.db-shm/-wal`, several `.DS_Store`.

---

## ⚠️ Measured: 69% of this corpus needs OCR

> **Correction.** An earlier version of this section concluded the corpus needed OCR on
> exactly **1 page in 5,384** and that the OCR question was therefore irrelevant. That was
> wrong. It measured whether a text layer *existed* (`len(text) < ocr_min_chars`), never
> whether it was *correct*. The text layer exists on almost every page — and for Sanskrit
> it is garbage. The corrected finding is below.

Many of these PDFs embed subset fonts with a broken or absent ToUnicode CMap. Extraction
"succeeds" and returns plenty of characters, but the Devanagari comes back spliced with
Latin letters, digits and raw C0 control bytes:

```
text layer : 'तB समासेना'टिवधा मूढगभ7गितdिI'टा'
should be  : 'तत्र समासेनाष्टविधा मूढगर्भगतिर्दृष्टा'
```

Testing text-layer *quality* (control bytes, private-use glyphs, U+FFFD, and Devanagari
abutting Latin) rather than length:

```
pages total                    : 5384
flagged corrupted              : 3739  (69%)
  with Devanagari              : 3739/4685   (80% of all Devanagari pages)
  WITHOUT Devanagari (suspect) : 0           (no false positives on English-only pages)
thin text layer (<40 chars)    : 1
=> pages that need OCR         : 3740  (69%)
```

Corruption is **not uniform**: 5 files (charak_part01–04, yogratnakar_part01) are entirely
clean, while the other 23 are 87–93% corrupted each.

OCR fixes it completely. Verified on three pages across three different books:

| Page | Text layer | Tesseract OCR |
|---|---|---|
| charak_part05 p166 | 433 Devanagari chars, 47 defects | 583 chars, **0 defects** |
| sushruta_part01 p130 | 560 Devanagari chars, 51 defects | 770 chars, **0 defects** |
| vagbhata_part01 p92 | 328 Devanagari chars, 40 defects | 464 chars, **0 defects** |

OCR also recovers ~35% *more* Devanagari than the broken encoding preserves — it isn't just
mangling characters, it's dropping them. Output length runs 103–104% of the text layer, so
nothing is truncated.

**Consequences:**

1. The `auto` trigger was fixed to test text-layer quality, not just length
   (`parser.looks_corrupted`) — without it, 69% of the corpus ingests as unusable Sanskrit.
2. **Tesseract is adequate here.** An earlier claim in this audit that it is "genuinely weak
   on Devanagari conjuncts" was overstated: on this printed material at 300 DPI it produces
   zero defects and coherent, correctly-conjuncted Sanskrit.
3. OCR costs ~5.2s/page → ~5.4h for the corpus, and is now the ingestion bottleneck.

Caveat: "0 defects" means no *decoding* artefacts. It cannot tell you whether the right
akshara was read — a confidently wrong character also scores 0. Sample output was reviewed
by the user against the page images and judged correct.

---

## OCR engine assessment

**Tesseract should be replaced** — it is genuinely weak on Devanagari conjunct characters,
which is most of this corpus, and the current OCR path fails silently on top of that
(0.4).

**`baidu/Unlimited-OCR` is the wrong fit here.** It's a 3B VLM built on DeepSeek-OCR,
MIT-licensed, tested on CUDA 12.9–13.0 / torch 2.10 / ≥8 GB VRAM, with **no documented CPU
or Apple Silicon path**. This project is moving to local-first on macOS. Its model card
also says only "multilingual" with no language list, so Devanagari/Sanskrit quality is
unverified — and 2026 OCR leaderboard figures in this space are vendor self-reported and
not independently reproduced.

> **⚠️ Correction — this machine is an Intel Mac.** Core i7-8750H (2018, 6-core), 16 GB,
> x86_64. The Apple Silicon / MLX argument below was a significant part of the original
> recommendation and **does not apply here**: MLX is Apple-Silicon-only, and there is no
> Neural Engine. On this hardware PaddleOCR-VL would run on a 2018 6-core CPU. Combined
> with the measurement above (1 page in 5,384 needs OCR), there is no case for the swap on
> this machine. The reasoning stands only for a future Apple Silicon or CUDA host.

**Recommended (on suitable hardware): PaddleOCR-VL 1.5** (also Baidu, 0.9B):

- 111 languages; the Devanagari category explicitly covers **Sanskrit, Hindi, Marathi,
  Nepali** — a direct match for this corpus rather than a hopeful guess.
- Apple Silicon path (**not applicable to this Intel host**): layout detection on the
  Paddle CPU backend, VLM inference via MLX-VLM with Metal acceleration.
- Emits structured markdown with tables and formulas, which would let us delete the fragile
  bbox-subtraction table logic in `parser.py:45-88` and give the chunker real paragraph
  boundaries (incidentally fixing 0.6).
- Runs as a server, keeping the heavy dependency out of the API process.

The right shape is an **OCR provider interface** — `TesseractOCR` retained as fallback,
`PaddleVLOCR` as default. Unlimited-OCR then drops in behind the same interface if a CUDA
box ever appears.

**Sequencing caveat:** OCR is not the biggest accuracy lever. Fixing 0.1, 0.3, 0.5 and the
RRF pool will move retrieval quality more, for a fraction of the work. OCR comes after.

### Sources
- [baidu/Unlimited-OCR (GitHub)](https://github.com/baidu/Unlimited-OCR) ·
  [model card](https://huggingface.co/baidu/Unlimited-OCR)
- [PaddleOCR-VL (Hugging Face)](https://huggingface.co/PaddlePaddle/PaddleOCR-VL) ·
  [Apple Silicon tutorial](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/PaddleOCR-VL-Apple-Silicon.html)
- [PaddleOCR-VL-1.5 paper](https://arxiv.org/pdf/2601.21957)
- [Open-source OCR VLM comparison, 2026](https://www.spheron.network/blog/best-open-source-ocr-vlm-self-host-gpu-cloud-2026/)

---

## Proposed phasing

| Phase | Status | Content |
|---|---|---|
| **A** | ✅ done | NVIDIA-only; Tier 0 accuracy fixes — see `docs/REVAMP.md` |
| **B** | ✅ done | Local-first: local Postgres + pgvector, local storage, single-user auth + remote-DB guard, stdio MCP entrypoint, CLI, migration runner, `config.py` SQLite path removed |
| **C** | ✅ done | Local embeddings (BAAI/bge-m3, 1024-d). Selectable via `PDFSPECS_EMBEDDING_BACKEND`; the NVIDIA backend is ~20x faster on this hardware and is what the corpus load actually uses |
| **D** | 🟡 partial | OCR. The **trigger fix shipped and was the critical part** — 69% of the corpus was ingesting as corrupted Sanskrit (see above). Tesseract is the working engine; the PaddleOCR backend is still non-functional (below), and Unlimited-OCR is unnecessary given Tesseract's measured output |
| **E** | ⏳ **next** | Tier 2 throughput (enrichment batching) + Tier 3 cleanup. The two-stage ingestion split landed early, as part of Phase E's Tier 2 scope; the rest is open — see the checklist below |

### Phase E — remaining work

**Tier 2 (throughput)**
- [x] Two-stage ingestion — store/search decoupled from enrichment (migration 0007)
- [x] Record-aware chunking — 88% of chunks now fit one embedding window
- [ ] **Batch enrichment** — 1 LLM call per chunk × ~29,000 chunks × 1.5s ≈ **12h**.
      Batching ~5 chunks/call plus raised concurrency brings it under 1h. Blocks summaries
      and `entity_lookup`.
- [ ] Retune `PDFSPECS_API_MIN_INTERVAL` — 1.5s was sized when each chunk made 2 API calls

**Tier 3 (correctness / hygiene)** — all still open
- [ ] Uploads fully buffered before the size check (`server.py`) — large POST OOMs before 413
- [ ] `reset_stuck_tasks()` called from two places, and is global rather than org-scoped
- [ ] HNSW index doesn't cover `org_id` — post-filtering hurts recall at multi-org scale
- [ ] Fernet decrypt failure returns `""`, indistinguishable from "no key configured"
- [ ] **No guard against mixed embedding models.** `chunks.embedding_model` is recorded but
      `search_semantic` doesn't filter on it. bge-m3 and nv-embedqa-e5-v5 are both 1024-d, so
      switching backends mid-corpus silently mixes two incompatible vector spaces — this
      nearly happened during the corpus load and was caught by hand
- [ ] ISSUE-1 — ingestion accepted with no provider key configured
- [ ] Tenant contextvar binding in `server.py` is safe only by accident of context copying
- [ ] Stray files: `temp.tsx`, `test_data.db-*`, `test_server_data.db-*`, `.DS_Store`

Test coverage: 32 tests (chunker, parser/OCR trigger, pgdb embedding guard). Still nothing
covering search, ingest, auth, or the server.

### Known issue — the PaddleOCR backend does not work

`src/ocr.py:PaddleOCRProvider` targets an older PaddleOCR API than the installed
paddleocr 3.7.0:

- `PaddleOCR(use_angle_cls=..., show_log=...)` — both removed/renamed in 3.x
  (`use_textline_orientation` is the replacement for the former)
- `.ocr(img, cls=True)` — superseded by `.predict()`
- `lang='devanagari'` — raises `ValueError: No models are available for lang='devanagari'`

It also builds *classic* PaddleOCR, not PaddleOCR-VL (the 0.9B VLM this audit actually
recommended) — a different model with different setup.

`get_ocr_provider` now degrades to Tesseract with an ERROR log rather than raising, and
`parse_pdf` constructs the provider lazily, so this no longer breaks ingestion. Until the
API is fixed, `ocr_provider = "tesseract"` in `config.json` is the honest setting.
