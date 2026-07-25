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


class AuthError(Exception):
    """Raised when a token/JWT cannot be authenticated."""


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
    """
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
