"""
Postgres data-access layer (multi-tenant).

Supersedes the SQLite src/db.py. Every function is scoped to an org_id — the tenant
boundary that keeps orgs' data separate. Lexical search uses Postgres full-text
(`tsvector`/`websearch_to_tsquery`) and semantic search uses pgvector cosine distance
(`<=>`) computed in the database against the HNSW index, so we never load a whole
corpus of embeddings into Python.

Connection: reads DATABASE_URL from the environment (Supabase pooled connection string).
Callsites (queue/mcp/server/cli) are migrated onto this module in M5/M6, once orgs exist.
"""
import os
import logging
from contextlib import contextmanager

import numpy as np
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector

logger = logging.getLogger("pdfspecs.pgdb")

# Fixed platform-wide embedding dimension (Gemini text-embedding-004). Must match the
# vector(N) column in db/migrations/0001_init_schema.sql.
EMBEDDING_DIM = 768

_pool: ConnectionPool | None = None


def _configure(conn):
    # Register the pgvector adapter so Python lists / numpy arrays map to `vector`.
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set (see .env.example).")
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=int(os.getenv("PDFSPECS_PG_POOL_MAX", "5")),
            # prepare_threshold=None disables client-side prepared statements, which
            # otherwise break under Supabase's transaction-mode pooler (pgbouncer).
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            configure=_configure,
            open=True,
        )
    return _pool


@contextmanager
def connection():
    """Yields a pooled, autocommit connection (dict rows, pgvector registered)."""
    with get_pool().connection() as conn:
        yield conn


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# =====================================================================
# Orgs (tenancy root) — used by the M3 auth bootstrap
# =====================================================================
def get_or_create_org(owner_auth_id: str, name: str = "My Organization") -> str:
    """Returns the org_id for a Google-authenticated user, creating it on first login."""
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO orgs (owner_auth_id, name) VALUES (%s, %s)
            ON CONFLICT (owner_auth_id) DO UPDATE SET owner_auth_id = EXCLUDED.owner_auth_id
            RETURNING id
            """,
            (owner_auth_id, name),
        ).fetchone()
        return str(row["id"])


def get_org_by_owner(owner_auth_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM orgs WHERE owner_auth_id = %s", (owner_auth_id,)
        ).fetchone()
        return dict(row) if row else None


# =====================================================================
# Documents
# =====================================================================
def add_document(org_id: str, filename: str, sha256: str, size_bytes: int) -> str:
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO documents (org_id, filename, sha256, size_bytes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, sha256)
                DO UPDATE SET filename = EXCLUDED.filename, size_bytes = EXCLUDED.size_bytes
            RETURNING id
            """,
            (org_id, filename, sha256, size_bytes),
        ).fetchone()
        return str(row["id"])


def get_document_by_sha256(org_id: str, sha256: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE org_id = %s AND sha256 = %s",
            (org_id, sha256),
        ).fetchone()
        return dict(row) if row else None


def list_documents(org_id: str) -> list:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE org_id = %s ORDER BY created_at DESC",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_document(org_id: str, doc_id: str):
    # Chunks/entities/keywords cascade via FK; the generated tsvector goes with the row.
    with connection() as conn:
        conn.execute(
            "DELETE FROM documents WHERE org_id = %s AND id = %s", (org_id, doc_id)
        )


# =====================================================================
# Chunks
# =====================================================================
def _clean_embedding(embedding):
    """Return a length-EMBEDDING_DIM list, or None if missing/wrong dimension."""
    if embedding is None or len(embedding) == 0:
        return None
    if len(embedding) != EMBEDDING_DIM:
        logger.warning(
            "Dropping embedding of dim %d (expected %d) before insert.",
            len(embedding), EMBEDDING_DIM,
        )
        return None
    return list(embedding)


def add_chunk(org_id, document_id, page_start, page_end, text_content, summary,
              embedding, embedding_model=None) -> str:
    emb = _clean_embedding(embedding)
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO chunks
                (org_id, document_id, page_start, page_end, text_content, summary,
                 embedding, embedding_model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (org_id, document_id, page_start, page_end, text_content, summary,
             emb, embedding_model),
        ).fetchone()
        return str(row["id"])


def get_chunk(org_id: str, chunk_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.page_start, c.page_end, c.summary, c.text_content, d.filename
            FROM chunks c JOIN documents d ON c.document_id = d.id
            WHERE c.org_id = %s AND c.id = %s
            """,
            (org_id, chunk_id),
        ).fetchone()
        return dict(row) if row else None


def get_chunks_by_ids(org_id: str, chunk_ids: list) -> dict:
    """Bulk-fetch chunk detail rows keyed by id (order preserved by the caller)."""
    if not chunk_ids:
        return {}
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.page_start, c.page_end, c.summary, c.text_content, d.filename
            FROM chunks c JOIN documents d ON c.document_id = d.id
            WHERE c.org_id = %s AND c.id = ANY(%s)
            """,
            (org_id, [str(c) for c in chunk_ids]),
        ).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            out[d["id"]] = d
        return out


# =====================================================================
# Search — lexical (tsvector) and semantic (pgvector), both org-scoped
# =====================================================================
def search_lexical(org_id: str, query_text: str, limit: int = 20) -> list:
    """
    Full-text search over the generated tsvector. `websearch_to_tsquery` accepts
    arbitrary user input (quotes, operators, punctuation) without raising, so no
    manual query sanitizing is needed.
    Returns [{"chunk_id": str, "rank": float}, ...] best first.
    """
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id AS chunk_id, ts_rank(tsv, q) AS rank
            FROM chunks, websearch_to_tsquery('simple', %s) q
            WHERE org_id = %s AND tsv @@ q
            ORDER BY rank DESC
            LIMIT %s
            """,
            (query_text, org_id, limit),
        ).fetchall()
        return [{"chunk_id": str(r["chunk_id"]), "rank": float(r["rank"])} for r in rows]


def search_semantic(org_id: str, query_embedding, limit: int = 20) -> list:
    """
    Nearest-neighbor search using pgvector cosine distance (`<=>`), computed in the DB
    against the HNSW index and scoped to the org. Score = 1 - distance (higher better).
    Returns [{"chunk_id": str, "score": float}, ...] best first.
    """
    emb = _clean_embedding(query_embedding)
    if emb is None:
        return []
    vec = np.asarray(emb, dtype=np.float32)
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id AS chunk_id, 1 - (embedding <=> %s) AS score
            FROM chunks
            WHERE org_id = %s AND embedding IS NOT NULL
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (vec, org_id, vec, limit),
        ).fetchall()
        return [{"chunk_id": str(r["chunk_id"]), "score": float(r["score"])} for r in rows]


# =====================================================================
# Entity graph
# =====================================================================
def add_entity(org_id, entity_id, name, entity_type):
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO entities (org_id, id, name, type) VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, id) DO NOTHING
            """,
            (org_id, entity_id, name, entity_type),
        )


def link_chunk_to_entity(org_id, chunk_id, entity_id):
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO chunk_entities (org_id, chunk_id, entity_id) VALUES (%s, %s, %s)
            ON CONFLICT (org_id, chunk_id, entity_id) DO NOTHING
            """,
            (org_id, chunk_id, entity_id),
        )


def add_keyword(org_id, chunk_id, keyword):
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO chunk_keywords (org_id, chunk_id, keyword) VALUES (%s, %s, %s)
            ON CONFLICT (org_id, chunk_id, keyword) DO NOTHING
            """,
            (org_id, chunk_id, keyword),
        )


def entity_lookup(org_id: str, entity_name: str, limit: int = 20) -> list:
    pattern = f"%{entity_name.strip().lower()}%"
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, c.page_start, c.page_end, c.summary,
                   d.filename, e.name AS entity_name, e.type AS entity_type
            FROM entities e
            JOIN chunk_entities ce ON e.org_id = ce.org_id AND e.id = ce.entity_id
            JOIN chunks c ON ce.org_id = c.org_id AND ce.chunk_id = c.id
            JOIN documents d ON c.document_id = d.id
            WHERE e.org_id = %s AND (lower(e.name) LIKE %s OR lower(e.id) LIKE %s)
            ORDER BY d.filename, c.page_start
            LIMIT %s
            """,
            (org_id, pattern, pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# =====================================================================
# Ingestion queue
# =====================================================================
def add_task(org_id: str, storage_key: str, original_filename: str = None) -> int:
    with connection() as conn:
        existing = conn.execute(
            """
            SELECT id FROM tasks
            WHERE org_id = %s AND storage_key = %s AND status IN ('pending', 'processing')
            """,
            (org_id, storage_key),
        ).fetchone()
        if existing:
            return int(existing["id"])
        row = conn.execute(
            """
            INSERT INTO tasks (org_id, storage_key, original_filename, status)
            VALUES (%s, %s, %s, 'pending') RETURNING id
            """,
            (org_id, storage_key, original_filename),
        ).fetchone()
        return int(row["id"])


def get_next_task() -> dict | None:
    """
    Atomically claim the oldest pending task across all orgs, marking it processing.
    `FOR UPDATE SKIP LOCKED` lets multiple workers dequeue safely without blocking.
    """
    with connection() as conn:
        with conn.transaction():
            task = conn.execute(
                """
                SELECT * FROM tasks WHERE status = 'pending'
                ORDER BY id ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            ).fetchone()
            if not task:
                return None
            conn.execute(
                "UPDATE tasks SET status = 'processing', attempts = attempts + 1 WHERE id = %s",
                (task["id"],),
            )
            return dict(task)


def update_task(task_id, status, document_id=None, error_message=None, progress_info=None):
    with connection() as conn:
        if progress_info is not None:
            conn.execute(
                """UPDATE tasks SET status=%s, document_id=%s, error_message=%s,
                   progress_info=%s WHERE id=%s""",
                (status, document_id, error_message, progress_info, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status=%s, document_id=%s, error_message=%s WHERE id=%s",
                (status, document_id, error_message, task_id),
            )


def update_task_progress(task_id, progress_info: str):
    with connection() as conn:
        conn.execute(
            "UPDATE tasks SET progress_info=%s WHERE id=%s", (progress_info, task_id)
        )


def list_tasks(org_id: str) -> list:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE org_id = %s ORDER BY created_at DESC", (org_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def reset_stuck_tasks() -> int:
    """Requeue tasks left 'processing' by a crashed worker; drop their partial docs."""
    with connection() as conn:
        stuck = conn.execute(
            "SELECT id, org_id, document_id FROM tasks WHERE status='processing' AND document_id IS NOT NULL"
        ).fetchall()
        for t in stuck:
            if t["document_id"]:
                try:
                    delete_document(str(t["org_id"]), str(t["document_id"]))
                except Exception as e:
                    logger.error("Cleanup of partial doc %s failed: %s", t["document_id"], e)
        cur = conn.execute(
            "UPDATE tasks SET status='pending', document_id=NULL WHERE status='processing'"
        )
        return cur.rowcount


# =====================================================================
# Per-org settings (provider key encrypted by the app before storing)
# =====================================================================
def save_setting(org_id: str, key: str, value: str, encrypt: bool = False):
    val = value
    if encrypt and value:
        from src.crypto_utils import encrypt_val
        val = encrypt_val(value)
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO org_settings (org_id, key, value) VALUES (%s, %s, %s)
            ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value
            """,
            (org_id, key, val),
        )


def get_setting(org_id: str, key: str, decrypt: bool = False) -> str:
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM org_settings WHERE org_id = %s AND key = %s", (org_id, key)
        ).fetchone()
        if not row:
            return ""
        val = row["value"]
        if decrypt and val:
            from src.crypto_utils import decrypt_val
            return decrypt_val(val)
        return val


# =====================================================================
# Agent API keys (only the hash is stored; raw shown once)
# =====================================================================
def add_api_key(org_id: str, token_hash: str, description: str):
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO api_keys (token_hash, org_id, description) VALUES (%s, %s, %s)
            ON CONFLICT (token_hash) DO NOTHING
            """,
            (token_hash, org_id, description),
        )


def verify_api_key(token_hash: str) -> str | None:
    """Return the org_id owning this token hash, or None if unknown."""
    with connection() as conn:
        row = conn.execute(
            "SELECT org_id FROM api_keys WHERE token_hash = %s", (token_hash,)
        ).fetchone()
        return str(row["org_id"]) if row else None


def list_api_keys(org_id: str) -> list:
    with connection() as conn:
        rows = conn.execute(
            "SELECT token_hash, description, created_at FROM api_keys WHERE org_id = %s ORDER BY created_at DESC",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_api_key(org_id: str, token_hash: str):
    with connection() as conn:
        conn.execute(
            "DELETE FROM api_keys WHERE org_id = %s AND token_hash = %s", (org_id, token_hash)
        )


# =====================================================================
# Token usage metrics (per org)
# =====================================================================
def record_token_usage(org_id: str, purpose: str, prompt_tokens: int, completion_tokens: int):
    total = prompt_tokens + completion_tokens
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO token_usage (org_id, purpose, prompt_tokens, completion_tokens, total_tokens)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (org_id, purpose, prompt_tokens, completion_tokens, total),
        )


def get_token_metrics(org_id: str) -> dict:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT purpose,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt,
                   COALESCE(SUM(completion_tokens), 0) AS completion,
                   COALESCE(SUM(total_tokens), 0)      AS total
            FROM token_usage WHERE org_id = %s GROUP BY purpose
            """,
            (org_id,),
        ).fetchall()
    metrics = {
        "ingestion": {"prompt": 0, "completion": 0, "total": 0},
        "sandbox": {"prompt": 0, "completion": 0, "total": 0},
    }
    for r in rows:
        if r["purpose"] in metrics:
            metrics[r["purpose"]] = {
                "prompt": int(r["prompt"]),
                "completion": int(r["completion"]),
                "total": int(r["total"]),
            }
    return metrics
