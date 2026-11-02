"""
Object storage for uploaded PDFs — pluggable backend (REVAMP Phase B).

Two backends behind one interface, chosen by `PDFSPECS_STORAGE_BACKEND`:

  * `local`    — files under `PDFSPECS_DATA_DIR/uploads/`. The default for local-first
                 use; no cloud account, no network round-trip on ingest.
  * `supabase` — a private Supabase Storage bucket, for hosted deployments whose local
                 disk is ephemeral. Authenticates with the Supabase secret key (service
                 role, bypasses storage RLS), so callers MUST pass a trusted org_id.

Both key objects the same way: `<org_id>/<uuid>-<safe-filename>`, keeping every org's
files under its own prefix. When unset, the backend defaults to `supabase` if SUPABASE_URL
is configured and `local` otherwise, so an existing hosted .env keeps working untouched.
"""
import os
import re
import uuid
import shutil
import logging
from pathlib import Path

import requests

logger = logging.getLogger("pdfspecs.storage")

DEFAULT_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "pdf-uploads")
# Upstream cap; the API layer enforces the configured max_upload_mb before calling.
MAX_UPLOAD_MB = int(os.getenv("PDFSPECS_MAX_UPLOAD_MB", "50"))


class StorageError(Exception):
    pass


# =====================================================================
# Backend selection
# =====================================================================
def backend() -> str:
    """Return the active backend name: 'local' or 'supabase'."""
    explicit = os.getenv("PDFSPECS_STORAGE_BACKEND", "").strip().lower()
    if explicit in ("local", "supabase"):
        return explicit
    if explicit:
        raise StorageError(
            f"Unknown PDFSPECS_STORAGE_BACKEND={explicit!r} (expected 'local' or 'supabase')."
        )
    return "supabase" if os.environ.get("SUPABASE_URL") else "local"


def data_dir() -> Path:
    """Root directory for locally stored files."""
    raw = os.getenv("PDFSPECS_DATA_DIR", "~/.pdfspecs/data")
    return Path(raw).expanduser().resolve()


def _uploads_root() -> Path:
    root = data_dir() / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_path(key: str) -> Path:
    """
    Resolve a storage key to a path inside the uploads root.

    Keys come from our own database, but resolving and re-checking containment costs
    nothing and means a malformed or crafted key can't escape the directory.
    """
    root = _uploads_root()
    candidate = (root / key).resolve()
    if not candidate.is_relative_to(root):
        raise StorageError(f"Storage key escapes the uploads directory: {key!r}")
    return candidate


# =====================================================================
# Supabase helpers
# =====================================================================
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


# =====================================================================
# Keys
# =====================================================================
def safe_filename(filename: str) -> str:
    """Reduce a user filename to its basename with only safe characters."""
    base = os.path.basename(filename or "").strip() or "upload.pdf"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base[:200]


def object_key(org_id: str, filename: str) -> str:
    """Build a unique, org-prefixed storage key for a new upload."""
    return f"{org_id}/{uuid.uuid4()}-{safe_filename(filename)}"


# =====================================================================
# Operations
# =====================================================================
def upload_bytes(org_id: str, filename: str, data: bytes,
                 content_type: str = "application/pdf") -> str:
    """Upload bytes to `<org_id>/<uuid>-<filename>`; returns the storage key."""
    key = object_key(org_id, filename)

    if backend() == "local":
        dest = _local_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info("Stored %d bytes at %s", len(data), dest)
        return key

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
    """Fetch a storage object to a local path (for the worker to parse)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if backend() == "local":
        src = _local_path(key)
        if not src.is_file():
            raise StorageError(f"Local object not found: {key}")
        # Copied rather than parsed in place so the worker's temp-dir cleanup can never
        # reach the stored original.
        shutil.copyfile(src, dest)
        return dest

    url = f"{_base_url()}/object/{_bucket()}/{key}"
    resp = requests.get(url, headers=_headers(), timeout=180)
    if not resp.ok:
        raise StorageError(f"Download failed ({resp.status_code}): {resp.text}")
    dest.write_bytes(resp.content)
    return dest


def delete_object(key: str) -> None:
    """Delete a storage object (best-effort cleanup)."""
    if backend() == "local":
        path = _local_path(key)
        if path.is_file():
            path.unlink()
        return

    url = f"{_base_url()}/object/{_bucket()}/{key}"
    resp = requests.delete(url, headers=_headers(), timeout=30)
    if not resp.ok:
        raise StorageError(f"Delete failed ({resp.status_code}): {resp.text}")
