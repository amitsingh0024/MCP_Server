import sqlite3
import numpy as np
from pathlib import Path
from src.config import get_config

def get_connection():
    config = get_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    # Use WAL so the background worker's writes don't block API/search reads,
    # and wait (rather than immediately erroring) when the DB is briefly locked.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Create documents table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        sha256 TEXT UNIQUE NOT NULL,
        size_bytes INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Create chunks table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        document_id TEXT,
        page_start INTEGER NOT NULL,
        page_end INTEGER NOT NULL,
        text_content TEXT NOT NULL,
        summary TEXT,
        embedding BLOB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
    );
    """)

    # Create entities table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL
    );
    """)

    # Create chunk_entities junction table (graph edges)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chunk_entities (
        chunk_id TEXT,
        entity_id TEXT,
        PRIMARY KEY (chunk_id, entity_id),
        FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
    );
    """)

    # Create chunk_keywords junction table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chunk_keywords (
        chunk_id TEXT,
        keyword TEXT,
        PRIMARY KEY (chunk_id, keyword),
        FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
    );
    """)

    # Create tasks table for queue
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT,
        file_path TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
        attempts INTEGER DEFAULT 0,
        error_message TEXT,
        progress_info TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE SET NULL
    );
    """)

    # Auto-migration for existing databases: try to add the column if it doesn't exist
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN progress_info TEXT DEFAULT '';")
    except sqlite3.OperationalError:
        pass # Column already exists

    # Create encrypted settings table
    cursor.execute("""

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)

    # Create agent api_keys table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS api_keys (
        token TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Create FTS5 virtual table for chunks
    try:
        cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            text_content,
            summary
        );
        """)
    except sqlite3.OperationalError as e:
        print(f"Warning: FTS5 virtual table creation failed: {e}. Lexical search might fall back.")

    # Create token_usage tracking table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purpose TEXT NOT NULL CHECK(purpose IN ('ingestion', 'sandbox')),
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()
    conn.close()

# Serialization helpers for embeddings
def serialize_embedding(vector):
    if vector is None:
        return None
    arr = np.array(vector, dtype=np.float32)
    return arr.tobytes()

def deserialize_embedding(blob):
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)

# CRUD operations for documents & chunks
def add_document(doc_id, filename, sha256, size_bytes):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO documents (id, filename, sha256, size_bytes) VALUES (?, ?, ?, ?)",
            (doc_id, filename, sha256, size_bytes)
        )

def get_document_by_sha256(sha256):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        return dict(row) if row else None

def list_documents():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

def delete_document(doc_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        chunk_ids = [r[0] for r in cursor.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,)).fetchall()]
        if chunk_ids:
            cursor.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(cid,) for cid in chunk_ids])
        cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

def add_chunk(chunk_id, document_id, page_start, page_end, text_content, summary, embedding):
    embedding_blob = serialize_embedding(embedding)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, document_id, page_start, page_end, text_content, summary, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, document_id, page_start, page_end, text_content, summary, embedding_blob)
        )
        try:
            conn.execute(
                "INSERT OR REPLACE INTO chunks_fts (chunk_id, text_content, summary) VALUES (?, ?, ?)",
                (chunk_id, text_content, summary)
            )
        except sqlite3.OperationalError:
            pass

def get_chunks_for_document(document_id):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM chunks WHERE document_id = ? ORDER BY page_start ASC", (document_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["embedding"] = deserialize_embedding(d["embedding"])
            result.append(d)
        return result

def get_all_chunks_with_embeddings():
    with get_connection() as conn:
        rows = conn.execute("SELECT id, document_id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
        result = []
        for r in rows:
            emb = deserialize_embedding(r["embedding"])
            if emb is not None and len(emb) > 0:
                result.append({
                    "id": r["id"],
                    "document_id": r["document_id"],
                    "embedding": emb
                })
        return result

def add_entity(entity_id, name, entity_type):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO entities (id, name, type) VALUES (?, ?, ?)",
            (entity_id, name, entity_type)
        )

def link_chunk_to_entity(chunk_id, entity_id):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chunk_entities (chunk_id, entity_id) VALUES (?, ?)",
            (chunk_id, entity_id)
        )

def add_keyword(chunk_id, keyword):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)",
            (chunk_id, keyword)
        )

def query_lexical_fts(query_text, limit=20):
    with get_connection() as conn:
        try:
            rows = conn.execute("""
                SELECT chunk_id, rank, snippet(chunks_fts, 1, '<b>', '</b>', '...', 64) as match_summary
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query_text, limit)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            search_pattern = f"%{query_text}%"
            rows = conn.execute("""
                SELECT id as chunk_id, 1.0 as rank, substr(text_content, 1, 200) as match_summary
                FROM chunks
                WHERE text_content LIKE ? OR summary LIKE ?
                LIMIT ?
            """, (search_pattern, search_pattern, limit)).fetchall()
            return [dict(r) for r in rows]

# CRUD operations for Settings (Encrypted)
def save_setting(key: str, value: str, encrypt: bool = False):
    if encrypt and value:
        from src.crypto_utils import encrypt_val
        val_to_save = encrypt_val(value)
    else:
        val_to_save = value
        
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, val_to_save)
        )

def get_setting(key: str, decrypt: bool = False) -> str:
    with get_connection() as conn:
        # Check if settings table exists first
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        if not table_exists:
            return ""
            
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return ""
        val = row["value"]
        if decrypt and val:
            from src.crypto_utils import decrypt_val
            return decrypt_val(val)
        return val

# CRUD operations for Agent API Keys
def add_api_key(token: str, description: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_keys (token, description) VALUES (?, ?)",
            (token, description)
        )

def list_api_keys():
    with get_connection() as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'"
        ).fetchone()
        if not table_exists:
            return []
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

def verify_api_key(token: str) -> bool:
    with get_connection() as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'"
        ).fetchone()
        if not table_exists:
            return False
        row = conn.execute("SELECT token FROM api_keys WHERE token = ?", (token,)).fetchone()
        return row is not None

def delete_api_key(token: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM api_keys WHERE token = ?", (token,))

def record_token_usage(purpose: str, prompt_tokens: int, completion_tokens: int):
    total_tokens = prompt_tokens + completion_tokens
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO token_usage (purpose, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?)",
            (purpose, prompt_tokens, completion_tokens, total_tokens)
        )

def get_token_metrics():
    with get_connection() as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
        ).fetchone()
        if not table_exists:
            return {"ingestion": {"prompt": 0, "completion": 0, "total": 0}, "sandbox": {"prompt": 0, "completion": 0, "total": 0}}
            
        ingest_row = conn.execute(
            "SELECT SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens) FROM token_usage WHERE purpose = 'ingestion'"
        ).fetchone()
        
        sandbox_row = conn.execute(
            "SELECT SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens) FROM token_usage WHERE purpose = 'sandbox'"
        ).fetchone()
        
        return {
            "ingestion": {
                "prompt": ingest_row[0] or 0,
                "completion": ingest_row[1] or 0,
                "total": ingest_row[2] or 0
            },
            "sandbox": {
                "prompt": sandbox_row[0] or 0,
                "completion": sandbox_row[1] or 0,
                "total": sandbox_row[2] or 0
            }
        }
