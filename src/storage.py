"""
Object storage for uploaded PDFs — Supabase Storage via its REST API.

Hosted deployments have an ephemeral local disk, so uploads go to a private bucket
instead. Objects are keyed by org: `<org_id>/<uuid>-<safe-filename>`, which keeps every
org's files under its own prefix. The backend authenticates with the Supabase secret key
(service role — bypasses storage RLS), so callers MUST pass a trusted org_id.
"""
import os
import re
import uuid
import logging
from pathlib import Path

import requests

logger = logging.getLogger("pdfspecs.storage")

DEFAULT_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "pdf-uploads")
# Upstream cap; the API layer (M5) enforces the configured max_upload_mb before calling.
MAX_UPLOAD_MB = int(os.getenv("PDFSPECS_MAX_UPLOAD_MB", "50"))


class StorageError(Exception):
    pass


def _bucket() -> str:
    return os.getenv("SUPABASE_STORAGE_BUCKET", DEFAULT_BUCKET)


def _base_url() -> str:
    url = os.environ.get("SUPABASE_URL")
    if not url:
        raise StorageError("SUPABASE_URL is not configured.")
    return url.rstrip("/") + "/storage/v1"


def _headers() -> dict:
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not key:
        raise StorageError("SUPABASE_SECRET_KEY is not configured.")
    return {"Authorization": f"Bearer {key}", "apikey": key}


def safe_filename(filename: str) -> str:
    """Reduce a user filename to its basename with only safe characters."""
    base = os.path.basename(filename or "").strip() or "upload.pdf"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base[:200]


def object_key(org_id: str, filename: str) -> str:
    """Build a unique, org-prefixed storage key for a new upload."""
    return f"{org_id}/{uuid.uuid4()}-{safe_filename(filename)}"


def upload_bytes(org_id: str, filename: str, data: bytes,
                 content_type: str = "application/pdf") -> str:
    """Upload bytes to `<org_id>/<uuid>-<filename>`; returns the storage key."""
    key = object_key(org_id, filename)
    url = f"{_base_url()}/object/{_bucket()}/{key}"
    resp = requests.post(
        url,
        headers={**_headers(), "Content-Type": content_type, "x-upsert": "true"},
        data=data,
        timeout=120,
    )
    if not resp.ok:
        raise StorageError(f"Upload failed ({resp.status_code}): {resp.text}")
    logger.info("Uploaded %d bytes to %s", len(data), key)
    return key


def download_to_path(key: str, dest: Path) -> Path:
    """Download a storage object to a local path (for the worker to parse)."""
    url = f"{_base_url()}/object/{_bucket()}/{key}"
    resp = requests.get(url, headers=_headers(), timeout=180)
    if not resp.ok:
        raise StorageError(f"Download failed ({resp.status_code}): {resp.text}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest


def delete_object(key: str) -> None:
    """Delete a storage object (best-effort cleanup)."""
    url = f"{_base_url()}/object/{_bucket()}/{key}"
    resp = requests.delete(url, headers=_headers(), timeout=30)
    if not resp.ok:
        raise StorageError(f"Delete failed ({resp.status_code}): {resp.text}")
