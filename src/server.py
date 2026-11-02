"""
OpenPDFSpecs — multi-tenant API + MCP server.

All data access is org-scoped. Two authenticated surfaces:
  * Admin dashboard   — Supabase user JWT (require_admin) → the caller's org.
  * MCP / agents      — org API key `sk-pdfspecs-...` (require_api_key / MCP middleware).

Everything runs on Postgres (pgdb), Supabase Storage (storage), the org-scoped worker
(ingest), and hybrid search (search). The legacy SQLite modules are gone.
"""
import os
import json
import logging
import threading
import contextvars
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import src.pgdb as pgdb
import src.auth as auth
import src.storage as storage
import src.ingest as ingest
import src.search as search
import src.enricher as enricher
import src.mcp_tools as mcp_tools
from src.config import get_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pdfspecs.server")


# ---------------------------------------------------------------------------
# App + lifespan (starts the background ingestion worker)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background ingestion worker (skippable so a web-only replica or local test doesn't
    # run a second worker against the shared DB).
    if auth.local_mode():
        logger.warning(
            "PDFSPECS_LOCAL_MODE is set: the admin API skips JWT verification and serves "
            "the implicit local org. Only loopback callers are accepted%s.",
            " (OVERRIDDEN — remote callers allowed)" if auth.allow_remote_local_mode() else "")
    if os.getenv("PDFSPECS_DISABLE_WORKER", "").lower() not in ("1", "true", "yes"):
        # run_worker_loop runs crash recovery itself, scoped to its own worker id — doing
        # it here as well would sweep with no identity at all.
        threading.Thread(target=ingest.run_worker_loop, daemon=True).start()
        logger.info("Background ingestion worker started.")
    else:
        logger.info("Worker disabled (PDFSPECS_DISABLE_WORKER).")
    # The Streamable HTTP session manager must be running to serve /mcp.
    async with mcp.session_manager.run():
        yield
    pgdb.close_pool()


app = FastAPI(title="OpenPDFSpecs API & MCP Server", lifespan=lifespan)

_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def require_admin(request: Request, authorization: str = Header(None)) -> dict:
    """
    Verify a Supabase user JWT and resolve (bootstrapping) the caller's org.

    Under PDFSPECS_LOCAL_MODE the JWT check is skipped entirely, which is only safe for a
    caller on this machine — so a non-loopback peer is refused here rather than handed
    admin. The app cannot see its own bind address (uvicorn takes --host on the command
    line, and the Dockerfile passes 0.0.0.0), so the check has to be per-request.
    """
    if auth.local_mode() and not auth.allow_remote_local_mode():
        peer = request.client.host if request.client else ""
        if not auth.client_is_loopback(peer):
            logger.error("Refused a local-mode admin request from %s", peer or "an unknown peer")
            raise HTTPException(status_code=403, detail=(
                "PDFSPECS_LOCAL_MODE bypasses authentication, so the admin API only "
                "answers callers on this machine. Unset PDFSPECS_LOCAL_MODE to use normal "
                "Supabase authentication, or set PDFSPECS_ALLOW_REMOTE_LOCAL_MODE=1 to "
                "accept remote unauthenticated callers deliberately."))
    token = _bearer(authorization)
    try:
        return auth.authenticate_admin(token)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=f"Unauthorized: {e}")


def require_api_key(authorization: str = Header(None)) -> str:
    """Resolve an org API key to its org_id."""
    org_id = auth.resolve_api_key(_bearer(authorization))
    if not org_id:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API key")
    return org_id


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin endpoints (Supabase JWT)
# ---------------------------------------------------------------------------
@app.get("/api/v1/me")
def get_me(admin: dict = Depends(require_admin)):
    return {"org_id": admin["org_id"], "email": admin["email"]}


@app.get("/api/v1/config")
def get_config_status(admin: dict = Depends(require_admin)):
    org = admin["org_id"]
    return {
        # NVIDIA NIM is the only provider (REVAMP Part A). Reported as a constant so the
        # frontend needs no provider-selection state.
        "provider": "nvidia",
        "has_nvidia_key": bool(pgdb.get_setting(org, "nvidia_api_key", decrypt=True)),
        "llm_model": pgdb.get_setting(org, "llm_model") or enricher.NVIDIA_LLM_MODEL,
        "embedding_model": enricher.default_embedding_model(),
        "embedding_backend": enricher.embedding_backend(),
    }


@app.post("/api/v1/config")
def save_config(payload: dict, admin: dict = Depends(require_admin)):
    org = admin["org_id"]
    payload = payload or {}
    if payload.get("nvidia_api_key"):
        pgdb.save_setting(org, "nvidia_api_key", payload["nvidia_api_key"], encrypt=True)
    if payload.get("llm_model"):
        pgdb.save_setting(org, "llm_model", str(payload["llm_model"]))
    # embedding_model is NOT settable: its dimension must match the vector(N) column.
    return {"status": "success"}


@app.get("/api/v1/documents")
def list_documents(admin: dict = Depends(require_admin)):
    return pgdb.list_documents(admin["org_id"])


@app.delete("/api/v1/documents/{doc_id}")
def delete_document(doc_id: str, admin: dict = Depends(require_admin)):
    # documents.id is a uuid column: an unknown or malformed id used to reach Postgres and
    # come back as a 500, or report success for a document that was never there.
    if not pgdb.delete_document(admin["org_id"], doc_id):
        raise HTTPException(status_code=404, detail=f"No such document: {doc_id}")
    return {"status": "success"}


@app.get("/api/v1/tasks")
def list_tasks(admin: dict = Depends(require_admin)):
    return pgdb.list_tasks(admin["org_id"])


@app.post("/api/v1/ingest")
async def ingest_document(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    org = admin["org_id"]
    max_bytes = get_config().max_upload_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413,
                            detail=f"File exceeds max upload size of {get_config().max_upload_mb} MB")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        key = storage.upload_bytes(org, file.filename or "upload.pdf", data)
        task_id = pgdb.add_task(org, key, file.filename)
    except Exception as e:
        logger.exception("Ingest upload failed")
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
    return {"status": "success", "task_id": task_id, "filename": file.filename}


@app.get("/api/v1/metrics")
def metrics(admin: dict = Depends(require_admin)):
    return pgdb.get_token_metrics(admin["org_id"])


# --- Org API key management (admin) ---
@app.get("/api/v1/keys")
def list_keys(admin: dict = Depends(require_admin)):
    # Never returns raw tokens — only hashes/descriptions.
    return pgdb.list_api_keys(admin["org_id"])


@app.post("/api/v1/keys")
def create_key(payload: dict = None, admin: dict = Depends(require_admin)):
    desc = (payload or {}).get("description", "Agent Access Key")
    raw = auth.generate_api_key(admin["org_id"], desc)
    # Raw token shown exactly once.
    return {"token": raw, "description": desc}


@app.delete("/api/v1/keys/{token_hash}")
def revoke_key(token_hash: str, admin: dict = Depends(require_admin)):
    pgdb.delete_api_key(admin["org_id"], token_hash)
    return {"status": "success"}


# --- Dashboard search (admin) ---
@app.post("/api/v1/search")
def admin_search(payload: dict, admin: dict = Depends(require_admin)):
    query = (payload or {}).get("query")
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query'")
    # clamp_limit, not int(): an unbounded limit asks Postgres for limit*10 candidate rows
    # from both indexes, and a non-numeric one used to 500 instead of being ignored.
    limit = search.clamp_limit((payload or {}).get("limit", search.DEFAULT_SEARCH_LIMIT))
    return {"results": search.hybrid_search(admin["org_id"], query, limit)}


# ---------------------------------------------------------------------------
# Agent REST search (org API key) — a simple alternative to MCP for HTTP agents
# ---------------------------------------------------------------------------
@app.post("/api/v1/agent/search")
def agent_search(payload: dict, org_id: str = Depends(require_api_key)):
    query = (payload or {}).get("query")
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query'")
    limit = search.clamp_limit((payload or {}).get("limit", search.DEFAULT_SEARCH_LIMIT))
    return {"results": search.hybrid_search(org_id, query, limit)}


# ---------------------------------------------------------------------------
# MCP over HTTP — tools scoped to the org resolved from the request's API key
# ---------------------------------------------------------------------------
_current_org: contextvars.ContextVar = contextvars.ContextVar("current_org", default=None)


def _mcp_org() -> str:
    org = _current_org.get()
    if not org:
        raise RuntimeError("No authenticated org in context.")
    return org


# FastMCP's DNS-rebinding protection defaults to allowing only localhost Host headers,
# which 421-rejects the platform hostname when hosted. Our MCPAuthMiddleware already
# authenticates every /mcp request with an org API key (unauthenticated callers get 401
# first), so the host check is redundant here. Allow it to be scoped via env, else disable.
_mcp_hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
if _mcp_hosts:
    _mcp_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_mcp_hosts,
        allowed_origins=[o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "*").split(",") if o.strip()],
    )
else:
    _mcp_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Stateless Streamable HTTP: one endpoint the client always calls with the SAME URL, so a
# `?token=` query param rides along on every request (unlike SSE, whose server-generated
# message endpoint dropped it). streamable_http_path="/" so the mount at "/mcp" yields "/mcp".
# json_response=True returns plain JSON for POSTs so clients that don't accept SSE still work.
mcp = FastMCP("OpenPDFSpecs", instructions=mcp_tools.MCP_INSTRUCTIONS,
              stateless_http=True, json_response=True,
              streamable_http_path="/", transport_security=_mcp_security)

# Tool bodies live in src/mcp_tools.py so this transport and the stdio one (src/mcp_server.py)
# can't drift apart. Here the org comes from the request's API key via the contextvar below.
mcp_tools.register_tools(mcp, _mcp_org)


class MCPAuthMiddleware:
    """Pure-ASGI middleware: authenticate the org API key and bind org_id to the context.

    Reads the key from `Authorization: Bearer` or a `?token=` query param (SSE-friendly).
    Rejects with 401 when missing/invalid. Runs in the same task as the wrapped app so the
    contextvar propagates to tool execution.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = _bearer(headers.get("authorization"))
        if not token:
            qs = parse_qs(scope.get("query_string", b"").decode())
            token = (qs.get("token") or [None])[0]
        org_id = auth.resolve_api_key(token) if token else None
        if not org_id:
            resp = JSONResponse({"detail": "Unauthorized: invalid or missing API key"}, status_code=401)
            return await resp(scope, receive, send)
        # Normalize the Accept header so the Streamable HTTP transport's strict checks pass
        # for lenient clients (some send only `application/json`, or omit it, and hit
        # "Not Acceptable: Client must accept text/event-stream"). The server still returns
        # JSON for POSTs (json_response=True); GET notification streams remain SSE.
        rewritten = [(k, v) for (k, v) in scope.get("headers", []) if k.decode().lower() != "accept"]
        rewritten.append((b"accept", b"application/json, text/event-stream"))
        scope = {**scope, "headers": rewritten}
        tok = _current_org.set(org_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_org.reset(tok)


_mcp_app = mcp.streamable_http_app()   # creates mcp.session_manager (run in lifespan)
app.mount("/mcp", MCPAuthMiddleware(_mcp_app))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.server:app",
                host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")),
                reload=False)
