"""
Authentication for the multi-tenant service — transport-agnostic (no FastAPI here,
so it stays unit-testable; the FastAPI dependencies wrap these in the M5/M6 server).

Two principals, both resolving to an org_id:

1. Admin (dashboard)      — a Supabase user JWT, verified against the project's JWKS.
                            First login bootstraps the caller's org (1 Google account = 1 org).
2. MCP/API consumer       — an org API key `sk-pdfspecs-...`. Only its SHA-256 hash is
                            stored; the raw value is shown once at creation.
"""
import os
import hashlib
import hmac
import logging
import secrets

import jwt
from jwt import PyJWKClient

import src.pgdb as pgdb

logger = logging.getLogger("pdfspecs.auth")

API_KEY_PREFIX = "sk-pdfspecs-"
# Supabase issues authenticated-user tokens with this audience.
JWT_AUDIENCE = "authenticated"
# Asymmetric algorithms advertised by Supabase's JWKS (new signing-key model).
JWT_ALGORITHMS = ["ES256", "RS256", "EdDSA"]

# --- Local single-user mode (REVAMP Phase B) -------------------------------
# With PDFSPECS_LOCAL_MODE set, there is no Supabase and no login: the process serves one
# implicit org owned by this fixed uid. `orgs.owner_auth_id` is a uuid column, so the
# sentinel is a valid UUID rather than a string like "local".
LOCAL_OWNER_AUTH_ID = "00000000-0000-0000-0000-000000000001"
LOCAL_ORG_NAME = "Local"
LOCAL_EMAIL = "local@localhost"


class AuthError(Exception):
    """Raised when a token/JWT cannot be authenticated."""


def local_mode() -> bool:
    """True when the process runs as a single-user local install (no Supabase auth)."""
    return os.getenv("PDFSPECS_LOCAL_MODE", "").strip().lower() in ("1", "true", "yes")


def allow_remote_local_mode() -> bool:
    """
    True when the operator has explicitly allowed local mode outside a local context.

    Local mode bypasses authentication entirely, so it is only safe when both the database
    and the callers are on this machine. This one override waives both checks — the remote
    database guard below, and the loopback-caller guard on the admin API.
    """
    return os.getenv("PDFSPECS_ALLOW_REMOTE_LOCAL_MODE", "").strip().lower() in (
        "1", "true", "yes")


# Hosts that count as "this machine". Anything else in DATABASE_URL is a remote database.
_LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1", "/tmp", "/var/run/postgresql"}

# Peer addresses that count as a caller on this machine, for the local-mode admin guard.
LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "localhost"}


def client_is_loopback(host: str) -> bool:
    """True when a request's peer address is this machine."""
    return (host or "").strip().lower() in LOOPBACK_CLIENTS


def _database_is_local(dsn: str) -> bool:
    """True when a Postgres DSN points at this machine (TCP localhost or a unix socket)."""
    if not dsn:
        return True                      # nothing configured; nothing to warn about
    from urllib.parse import urlparse
    try:
        host = (urlparse(dsn).hostname or "").strip().lower()
    except Exception:
        return False                     # unparseable: treat as remote, i.e. be cautious
    return host in _LOCAL_DB_HOSTS


def assert_local_database():
    """
    Refuse to run local-mode commands against a REMOTE database.

    Local mode bypasses authentication entirely and serves one implicit org. Pointed at a
    hosted database it silently creates that org there and writes secrets into production —
    which is exactly what happened when PDFSPECS_LOCAL_MODE was set in the shell while
    DATABASE_URL still resolved to Supabase from .env.
    """
    if not local_mode():
        return
    dsn = os.environ.get("DATABASE_URL", "")
    if _database_is_local(dsn):
        return
    from urllib.parse import urlparse
    host = urlparse(dsn).hostname or "?"
    raise AuthError(
        f"Refusing to run in local mode against the remote database at '{host}'.\n"
        f"PDFSPECS_LOCAL_MODE=1 bypasses authentication and would create the implicit "
        f"local org there.\n"
        f"Set DATABASE_URL to a local database (e.g. postgresql:///pdfspecs), or unset "
        f"PDFSPECS_LOCAL_MODE to use normal authentication.\n"
        f"Override deliberately with PDFSPECS_ALLOW_REMOTE_LOCAL_MODE=1."
    )


def local_org_id() -> str:
    """The org_id of the implicit local org, creating it on first use."""
    if not allow_remote_local_mode():
        assert_local_database()
    return pgdb.get_or_create_org(LOCAL_OWNER_AUTH_ID, name=LOCAL_ORG_NAME)


# =====================================================================
# Admin — Supabase user JWT (verified via JWKS)
# =====================================================================
_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        url = os.environ.get("SUPABASE_JWKS_URL")
        if not url:
            raise AuthError("SUPABASE_JWKS_URL is not configured.")
        # PyJWKClient caches fetched keys (with its own TTL).
        _jwk_client = PyJWKClient(url)
    return _jwk_client


def verify_supabase_jwt(token: str) -> dict:
    """
    Verify a Supabase user JWT using the project's JWKS. Returns the decoded claims.
    Raises AuthError on any failure (bad signature, expired, wrong audience, ...).
    """
    if not token:
        raise AuthError("Missing bearer token.")
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=JWT_ALGORITHMS,
            audience=JWT_AUDIENCE,
            options={"require": ["exp", "sub"]},
        )
    except AuthError:
        raise
    except Exception as e:
        # Normalize every JWT/JWKS error into one auth failure type.
        raise AuthError(f"Invalid token: {e}") from e


def authenticate_admin(token: str) -> dict:
    """
    Verify the admin's JWT and resolve (bootstrapping on first login) their org.
    Returns {"auth_uid": str, "email": str, "org_id": str}.

    In local mode the token is ignored entirely and the implicit local org is returned —
    there is no Supabase project to verify against.

    That makes this a FULL authentication bypass, safe only for a caller on this machine.
    Nothing here can see the bind address (under uvicorn/gunicorn it comes from the command
    line), so the transport must reject non-loopback callers before calling this — see
    client_is_loopback() and the server's require_admin.
    """
    if local_mode():
        return {"auth_uid": LOCAL_OWNER_AUTH_ID, "email": LOCAL_EMAIL,
                "org_id": local_org_id()}

    claims = verify_supabase_jwt(token)
    auth_uid = claims.get("sub")
    if not auth_uid:
        raise AuthError("Token has no subject (sub) claim.")
    email = claims.get("email", "") or ""
    org_id = pgdb.get_or_create_org(auth_uid, name=email or "My Organization")
    return {"auth_uid": auth_uid, "email": email, "org_id": org_id}


# =====================================================================
# MCP/API consumer — org API keys
# =====================================================================
def hash_token(raw: str) -> str:
    """SHA-256 hex of a raw API token (what we persist)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key(org_id: str, description: str = "Agent Access Key") -> str:
    """
    Mint a new org-scoped API key. Stores only the hash; returns the raw token ONCE
    (the caller must surface it to the user — it can't be recovered later).
    """
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    pgdb.add_api_key(org_id, hash_token(raw), description)
    return raw


def resolve_api_key(raw: str) -> str | None:
    """Return the org_id owning a raw API key, or None if unknown/malformed."""
    if not raw or not raw.startswith(API_KEY_PREFIX):
        return None
    return pgdb.verify_api_key(hash_token(raw))


def constant_time_equals(a: str, b: str) -> bool:
    """Constant-time string compare (for any place that compares secrets directly)."""
    return hmac.compare_digest(a, b)
