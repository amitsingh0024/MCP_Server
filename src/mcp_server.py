"""
stdio MCP server (REVAMP Phase B).

The counterpart to the HTTP transport in src/server.py. Local MCP clients — Claude Code,
Claude Desktop, Cursor — launch a subprocess and speak JSON-RPC over stdin/stdout, which
needs no port, no API key round-trip, and no hosting. This is the entrypoint that made
local-first use possible again; the HTTP surface remains for remote clients.

Org resolution:
  * PDFSPECS_LOCAL_MODE=1  → the implicit single-user local org (the normal local setup).
  * otherwise              → PDFSPECS_API_KEY, resolved to its owning org, so this can
                             also front a remote/hosted database.

Register with Claude Code:
    claude mcp add openpdfspecs -- /abs/path/.venv/bin/python -m src.mcp_server
"""
import os
import sys
import logging

from mcp.server.fastmcp import FastMCP

from src.config import load_dotenv_if_present

# Load .env BEFORE importing anything that reads the environment at import time (pgdb's
# EMBEDDING_DIM, storage's backend selection). An MCP client launches this process without
# a shell profile, so the .env is the only place these values come from.
load_dotenv_if_present()

import src.auth as auth              # noqa: E402  - must follow load_dotenv_if_present()
import src.pgdb as pgdb              # noqa: E402
import src.storage as storage        # noqa: E402
import src.mcp_tools as mcp_tools    # noqa: E402

# CRITICAL: logging must go to stderr. stdout carries the JSON-RPC frames, and any stray
# byte written there corrupts the stream and breaks the client connection.
logging.basicConfig(
    level=os.getenv("PDFSPECS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("pdfspecs.mcp_server")


def resolve_org() -> str:
    """Resolve the org this server serves. Fixed for the process lifetime."""
    if auth.local_mode():
        return auth.local_org_id()

    api_key = os.getenv("PDFSPECS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "No org configured. Set PDFSPECS_LOCAL_MODE=1 for a local single-user "
            "install, or PDFSPECS_API_KEY=sk-pdfspecs-... to serve a specific org."
        )
    org_id = auth.resolve_api_key(api_key)
    if not org_id:
        raise RuntimeError("PDFSPECS_API_KEY is not a valid API key.")
    return org_id


def build_server() -> FastMCP:
    org_id = resolve_org()
    logger.info("Serving org %s over stdio (storage backend: %s)", org_id, storage.backend())

    mcp = FastMCP("OpenPDFSpecs", instructions=mcp_tools.MCP_INSTRUCTIONS)
    # The org is fixed at startup, so the resolver is a constant — unlike the HTTP
    # transport, where it varies per request with the caller's API key.
    mcp_tools.register_tools(mcp, lambda: org_id)
    return mcp


def main() -> int:
    try:
        mcp = build_server()
    except Exception as e:
        # A failure here means the client sees an immediate exit; make the reason visible
        # on stderr, which is what the client surfaces in its logs.
        logger.error("Startup failed: %s", e)
        return 1

    try:
        mcp.run(transport="stdio")
    finally:
        pgdb.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
