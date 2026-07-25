# Pending Issues

Running list of issues to address. Newest at the top. Delete an entry (or mark ✅ Fixed)
once resolved. This is a lightweight tracker — not every item needs a big fix.

Status key: 🔴 Open · 🟡 In progress · ✅ Fixed

---

## ISSUE-3 — MCP URL-token auth doesn't cover the SSE message channel
- **Status:** ✅ Fixed — switched to stateless Streamable HTTP (token-in-URL works). Verified locally.
- **What happens:** With the key in the URL (`/mcp/sse?token=…`), the initial SSE GET
  authenticates, but the client then POSTs to the server-generated `/mcp/messages/?session_id=…`
  URL, which has no token → `MCPAuthMiddleware` returns 401 and the handshake fails.
  Header auth (`Authorization: Bearer`) works because the client sends it on every request.
- **Impact:** Clients that only accept a URL (e.g. claude.ai custom connectors) can't connect.
  Header-capable clients (Claude Code `--header`, Cursor headers, `mcp-remote --header`) work.
- **Fix options:**
  1. Switch the MCP transport from SSE to **Streamable HTTP** (`mcp.streamable_http_app()`) —
     single endpoint, header auth on every request, and it's what modern connectors expect.
  2. Session-based auth: authenticate at SSE connect, map `session_id` → org, and don't
     re-require the token on the message channel.
  - (1) is preferred — simpler and more compatible.
- **Verified:** live handshake test against Render — header-auth=True, url-token=False (401
  on `/mcp/messages/`).

---

## ISSUE-2 — NVIDIA free-tier rate limit (429) during ingestion
- **Status:** ✅ Fixed (commit adds a global request throttle)
- **What happened:** Parallel enrichment (4 workers × LLM+embedding per chunk) burst past
  NVIDIA's free-tier rate limit → repeated `429 Too Many Requests`, slowing/stalling ingest.
- **Fix:** Global thread-safe throttle in `enricher._post_with_retry` spacing all provider
  API calls (`PDFSPECS_API_MIN_INTERVAL`, default 1.5s ≈ 40/min); default enrichment
  concurrency dropped to 1. Ingestion is slower but reliable (acceptable — one-time).

---

## ISSUE-1 — Ingestion allowed before provider key is configured
- **Status:** 🔴 Open
- **Severity:** Medium (UX / confusing failure)
- **What happens:** A user can upload a PDF on the Ingest tab *before* setting a provider
  key in Settings. The task runs with the default provider (`gemini`), finds no key, and
  fails with `"Gemini API key is missing"` — even if the user intended NVIDIA. The task
  burns through its retry cap and lands in `failed`, which is confusing.
- **Root cause:** The `/api/v1/ingest` endpoint accepts uploads unconditionally; provider
  defaults to `gemini`; there's no pre-check that the org has a usable provider key.
- **Proposed fix (options):**
  1. Backend guard: `/api/v1/ingest` returns 400 with a clear message
     ("Configure a provider key in Settings first") when the org has no key for its
     selected provider.
  2. Frontend guard: disable the upload control + show a hint until a key is saved
     (surface `has_gemini_key`/`has_nvidia_key` from `/api/v1/config`).
  3. Ideally both — backend guard is the source of truth; frontend gives early feedback.
- **Files:** `src/server.py` (`ingest_document`), `frontend/src/app/page.tsx`
  (`IngestPanel` / `SettingsPanel`).
