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
import uuid
import logging
from contextlib import contextmanager

import numpy as np
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector

logger = logging.getLogger("pdfspecs.pgdb")

# Platform-wide embedding dimension. Must match the vector(N) column in the schema —
# 1024 (migrations 0003 / 0005). Both supported backends meet it: BAAI/bge-m3 natively,
# and the NVIDIA model via Matryoshka reduction (PDFSPECS_NVIDIA_EMBED_DIMENSIONS).
EMBEDDING_DIM = int(os.getenv("PDFSPECS_EMBEDDING_DIM", "1024"))

_pool: ConnectionPool | None = None


def is_valid_uuid(value) -> bool:
    """
    True when `value` is a well-formed UUID.

    documents.id and chunks.id are `uuid` columns, and ids reach us from places we don't
    control: an MCP tool argument (i.e. straight from a model, which will occasionally
    invent one) or a URL path. Postgres rejects a non-UUID string with `invalid input
    syntax for type uuid`, which surfaced as an unhandled exception out of the MCP tool
    instead of the plain "not found" the tool already knew how to return.
    """
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


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


def delete_document(org_id: str, doc_id: str) -> bool:
    """
    Delete one document. Returns True when a row was actually removed.

    Chunks/entities cascade via FK; the generated tsvector goes with the row. The return
    value lets the API answer 404 for an unknown id instead of 200, and a malformed id
    is a miss rather than a uuid cast error.
    """
    if not is_valid_uuid(doc_id):
        return False
    with connection() as conn:
        cur = conn.execute(
            "DELETE FROM documents WHERE org_id = %s AND id = %s", (org_id, doc_id)
        )
        return cur.rowcount > 0


# =====================================================================
# Chunks
# =====================================================================
class EmbeddingDimError(ValueError):
    """A vector's dimension doesn't match the platform's vector(N) column."""


def _clean_embedding(embedding):
    """
    Return a length-EMBEDDING_DIM list, or None if the vector is absent.

    A WRONG dimension raises rather than returning None. Silently dropping mismatched
    vectors is what let the whole corpus ingest "successfully" with zero searchable
    embeddings — the ingest reported success and semantic search returned nothing, with
    only a log line to say why (REVAMP B-1). An absent vector stays a legitimate,
    non-fatal state: the provider call failed and the chunk degrades to lexical-only.
    """
    if embedding is None or len(embedding) == 0:
        return None
    if len(embedding) != EMBEDDING_DIM:
        raise EmbeddingDimError(
            f"Embedding has dimension {len(embedding)}, but this deployment's "
            f"chunk_embeddings.embedding column is vector({EMBEDDING_DIM}). "
            f"Check the embedding model and PDFSPECS_EMBEDDING_DIM."
        )
    return list(embedding)


def add_chunk(org_id, document_id, page_start, page_end, text_content, summary,
              embeddings=None, embedding_model=None) -> str:
    """
    Insert a chunk plus its window vectors.

    `embeddings` is a LIST of vectors (one per embedding window) — see enricher._windows.
    They go into chunk_embeddings, one row each, in the same transaction as the chunk so a
    failure can't leave a chunk with a partial vector set.
    """
    vectors = [_clean_embedding(e) for e in (embeddings or [])]
    vectors = [v for v in vectors if v is not None]
    with connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO chunks
                    (org_id, document_id, page_start, page_end, text_content, summary,
                     embedding_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (org_id, document_id, page_start, page_end, text_content, summary,
                 embedding_model),
            ).fetchone()
            chunk_id = str(row["id"])
            for idx, vec in enumerate(vectors):
                conn.execute(
                    """
                    INSERT INTO chunk_embeddings (org_id, chunk_id, window_index, embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chunk_id, window_index) DO UPDATE
                        SET embedding = EXCLUDED.embedding
                    """,
                    (org_id, chunk_id, idx, np.asarray(vec, dtype=np.float32)),
                )
        return chunk_id


def add_chunks_bulk(org_id, document_id, rows: list) -> list:
    """
    Insert many chunks and their window vectors in ONE transaction.

    `rows` is [{page_start, page_end, text_content, summary, embeddings, embedding_model}].
    Returns the new chunk ids in input order.

    The per-chunk add_chunk() took a pooled connection per chunk and again per embedding
    window — roughly 35,000 round-trips for a full corpus. Here the whole document is one
    transaction with two executemany calls, so a crash mid-document also can't leave half a
    document behind.
    """
    if not rows:
        return []

    chunk_ids = []
    with connection() as conn:
        with conn.transaction():
            for r in rows:
                row = conn.execute(
                    """
                    INSERT INTO chunks
                        (org_id, document_id, page_start, page_end, text_content, summary,
                         embedding_model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (org_id, document_id, r["page_start"], r["page_end"],
                     r["text_content"], r.get("summary"), r.get("embedding_model")),
                ).fetchone()
                chunk_ids.append(str(row["id"]))

            vector_rows = []
            for chunk_id, r in zip(chunk_ids, rows):
                for idx, vec in enumerate(r.get("embeddings") or []):
                    clean = _clean_embedding(vec)
                    if clean is not None:
                        vector_rows.append(
                            (org_id, chunk_id, idx, np.asarray(clean, dtype=np.float32)))

            if vector_rows:
                conn.cursor().executemany(
                    """
                    INSERT INTO chunk_embeddings (org_id, chunk_id, window_index, embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chunk_id, window_index) DO UPDATE
                        SET embedding = EXCLUDED.embedding
                    """,
                    vector_rows,
                )
    return chunk_ids


# =====================================================================
# Enrichment (stage 2) — chunks land unenriched, summaries/entities backfill later
# =====================================================================
def chunks_pending_enrichment(org_id: str, limit: int = 100) -> list:
    """Oldest chunks still awaiting stage-2 enrichment."""
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, text_content FROM chunks
            WHERE org_id = %s AND enriched_at IS NULL
            ORDER BY created_at, id
            LIMIT %s
            """,
            (org_id, limit),
        ).fetchall()
        return [{"id": str(r["id"]), "text_content": r["text_content"]} for r in rows]


def corpus_embedding_models(org_id: str) -> list:
    """Distinct embedding models present in this org's stored chunks."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT embedding_model FROM chunks "
            "WHERE org_id = %s AND embedding_model IS NOT NULL",
            (org_id,),
        ).fetchall()
        return [r["embedding_model"] for r in rows]


def orgs_with_pending_enrichment() -> list:
    """Org ids that still have unenriched chunks (used by the idle worker loop)."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT org_id FROM chunks WHERE enriched_at IS NULL"
        ).fetchall()
        return [str(r["org_id"]) for r in rows]


def count_pending_enrichment(org_id: str) -> tuple:
    """Return (pending, total) chunk counts for the org."""
    with connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) FILTER (WHERE enriched_at IS NULL) AS pending,
                   count(*)                                     AS total
            FROM chunks WHERE org_id = %s
            """,
            (org_id,),
        ).fetchone()
        return int(row["pending"]), int(row["total"])


def save_enrichment(org_id: str, chunk_id: str, summary: str, entities: list):
    """
    Attach a summary and entities to an already-stored chunk and mark it enriched.

    One transaction so a chunk is never marked enriched without its entities. Setting
    `summary` also refreshes the generated `tsv` column, so the lexical index picks the
    summary up automatically.
    """
    with connection() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE chunks SET summary = %s, enriched_at = now() "
                "WHERE org_id = %s AND id = %s",
                (summary or "", org_id, chunk_id),
            )
            for ent in entities or []:
                name = (ent.get("name") or "").strip()
                if not name:
                    continue
                etype = ent.get("type", "concept")
                ent_id = f"{etype}:{name.lower()}"
                conn.execute(
                    "INSERT INTO entities (org_id, id, name, type) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (org_id, id) DO NOTHING",
                    (org_id, ent_id, name, etype),
                )
                conn.execute(
                    "INSERT INTO chunk_entities (org_id, chunk_id, entity_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT (org_id, chunk_id, entity_id) DO NOTHING",
                    (org_id, chunk_id, ent_id),
                )


def mark_enrichment_failed(org_id: str, chunk_id: str):
    """
    Mark a chunk enriched-with-nothing so a permanently failing chunk can't wedge the pass.

    The chunk stays fully searchable — it has its text and embeddings from stage 1; it just
    never gets a summary.
    """
    with connection() as conn:
        conn.execute(
            "UPDATE chunks SET enriched_at = now() WHERE org_id = %s AND id = %s",
            (org_id, chunk_id),
        )


def get_chunk(org_id: str, chunk_id: str) -> dict | None:
    if not is_valid_uuid(chunk_id):
        return None                      # a malformed id is a miss, not a uuid cast error
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
    # One malformed id would fail the whole batch on the uuid cast, so drop those instead.
    valid = [str(c) for c in (chunk_ids or []) if is_valid_uuid(c)]
    if not valid:
        return {}
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.page_start, c.page_end, c.summary, c.text_content, d.filename
            FROM chunks c JOIN documents d ON c.document_id = d.id
            WHERE c.org_id = %s AND c.id = ANY(%s)
            """,
            (org_id, valid),
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

    The query is the OR (`||` on tsquery) of an `english` and a `simple` parse, matching
    the union tsvector built by migration 0006. The english side supplies stemming, so
    "fevers" finds "fever"; the simple side preserves Devanagari and transliterated terms
    verbatim, which the english dictionary would mangle. Indexing only `simple` — the
    original behaviour — meant no English query ever matched a morphological variant.

    Returns [{"chunk_id": str, "rank": float}, ...] best first.
    """
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id AS chunk_id, ts_rank(tsv, qq.q) AS rank
            FROM chunks,
                 (SELECT websearch_to_tsquery('english', %s)
                         || websearch_to_tsquery('simple', %s) AS q) qq
            WHERE org_id = %s AND tsv @@ qq.q
            ORDER BY rank DESC
            LIMIT %s
            """,
            (query_text, query_text, org_id, limit),
        ).fetchall()
        return [{"chunk_id": str(r["chunk_id"]), "rank": float(r["rank"])} for r in rows]


def search_semantic(org_id: str, query_embedding, limit: int = 20) -> list:
    """
    Nearest-neighbor search using pgvector cosine distance (`<=>`), computed in the DB
    against the HNSW index and scoped to the org. Score = 1 - distance (higher better).

    Searches WINDOW vectors and scores each chunk by its best-matching window (max-sim).
    A chunk is relevant when any part of it is relevant; averaging its windows into one
    vector — the old behaviour — made long chunks progressively harder to find, since the
    matching passage got diluted by everything around it.

    Returns [{"chunk_id": str, "score": float}, ...] best first.
    """
    emb = _clean_embedding(query_embedding)
    if emb is None:
        return []
    vec = np.asarray(emb, dtype=np.float32)
    # Over-fetch windows: several may belong to one chunk and collapse in the GROUP BY.
    window_limit = max(limit * 4, limit)
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT chunk_id, max(score) AS score FROM (
                SELECT chunk_id, 1 - (embedding <=> %s) AS score
                FROM chunk_embeddings
                WHERE org_id = %s
                ORDER BY embedding <=> %s
                LIMIT %s
            ) w
            GROUP BY chunk_id
            ORDER BY score DESC
            LIMIT %s
            """,
            (vec, org_id, vec, window_limit, limit),
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


# add_keyword() removed with the chunk_keywords table (migration 0007): it was write-only —
# nothing in the codebase ever read a keyword back. Entity lookup uses entities /
# chunk_entities, below.


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


def get_next_task(worker_id: str = None) -> dict | None:
    """
    Atomically claim the oldest pending task across all orgs, marking it processing.
    `FOR UPDATE SKIP LOCKED` lets multiple workers dequeue safely without blocking.

    `worker_id` is stamped on the row so crash recovery can tell this worker's abandoned
    tasks from another worker's live ones (migration 0008).
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
                "UPDATE tasks SET status = 'processing', attempts = attempts + 1, "
                "locked_by = %s WHERE id = %s",
                (worker_id, task["id"]),
            )
            return dict(task)


def update_task(task_id, status, document_id=None, error_message=None, progress_info=None):
    """
    Move a task to `status`.

    The lease (locked_by) is released whenever the task leaves 'processing', and kept when
    it stays there — ingest re-calls this mid-run to attach document_id, and clearing the
    owner then would make a live task look reclaimable to every other worker.
    """
    with connection() as conn:
        if progress_info is not None:
            conn.execute(
                """UPDATE tasks SET status=%s, document_id=%s, error_message=%s,
                   progress_info=%s,
                   locked_by = CASE WHEN %s::text = 'processing' THEN locked_by ELSE NULL END
                   WHERE id=%s""",
                (status, document_id, error_message, progress_info, status, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status=%s, document_id=%s, error_message=%s, "
                "locked_by = CASE WHEN %s::text = 'processing' THEN locked_by ELSE NULL END "
                "WHERE id=%s",
                (status, document_id, error_message, status, task_id),
            )


def update_task_progress(task_id, progress_info: str):
    """
    Write a progress snapshot — and, as a side effect, renew the task's lease.

    The trg_tasks_updated_at trigger refreshes updated_at on any UPDATE, so every progress
    tick proves the worker is alive and keeps reset_stuck_tasks from reclaiming the task.
    """
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


# How long a 'processing' task may go without a progress update before another worker may
# treat it as abandoned. Every ingest._progress() call refreshes updated_at (see
# update_task_progress), so this only expires for a worker that has actually stopped.
TASK_LEASE_MINUTES = int(os.getenv("PDFSPECS_TASK_LEASE_MINUTES", "15"))


def reset_stuck_tasks(worker_id: str = None, lease_minutes: int = None) -> int:
    """
    Requeue tasks abandoned by a crashed worker; drop their partial docs.

    Reclaims ONLY tasks that are genuinely abandoned (migration 0008):
      * claimed by this same worker_id — i.e. by this process's previous life, which is
        safe to assume dead at startup;
      * with no owner recorded, from a build predating the lease column;
      * whose lease has gone stale (no progress update for `lease_minutes`).

    It used to reset every 'processing' task across all orgs and delete each one's
    document, so starting a second worker destroyed the document the first was writing.
    """
    minutes = TASK_LEASE_MINUTES if lease_minutes is None else lease_minutes
    with connection() as conn:
        stuck = conn.execute(
            """
            SELECT id, org_id, document_id FROM tasks
            WHERE status = 'processing'
              AND (locked_by IS NULL
                   OR locked_by = %(worker)s
                   OR updated_at < now() - make_interval(mins => %(minutes)s))
            """,
            {"worker": worker_id, "minutes": minutes},
        ).fetchall()
        if not stuck:
            return 0
        # Release exactly the rows that were examined, by id — the old code selected one
        # set and updated a wider one, so the returned count never matched the cleanup.
        conn.execute(
            "UPDATE tasks SET status='pending', document_id=NULL, locked_by=NULL "
            "WHERE id = ANY(%s)",
            ([t["id"] for t in stuck],),
        )

    # Partial documents are dropped only AFTER the task rows are released. delete_document
    # takes its own pooled connection, and tasks.document_id is ON DELETE SET NULL, so
    # deleting while holding a lock on those task rows deadlocks the pool against itself.
    for t in stuck:
        if t["document_id"]:
            try:
                delete_document(str(t["org_id"]), str(t["document_id"]))
            except Exception as e:
                logger.error("Cleanup of partial doc %s failed: %s", t["document_id"], e)
    logger.info("Requeued %d abandoned task(s).", len(stuck))
    return len(stuck)


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
