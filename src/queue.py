import time
import uuid
import logging
from pathlib import Path
import sqlite3
from src.config import get_config
import src.db as db
import src.parser as parser
import src.chunker as chunker
import src.enricher as enricher

logger = logging.getLogger("pdfspecs.queue")

def add_task(file_path: str) -> int:
    """Adds a file path to the pending ingestion queue."""
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
        
    with db.get_connection() as conn:
        # Check if already in queue with pending/processing
        existing = conn.execute(
            "SELECT id FROM tasks WHERE file_path = ? AND status IN ('pending', 'processing')",
            (str(path),)
        ).fetchone()
        if existing:
            return existing[0]

        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (file_path, status) VALUES (?, 'pending')",
            (str(path),)
        )
        return cursor.lastrowid

def get_next_task():
    """Fetches and locks the next pending task, marking it as processing."""
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            task = conn.execute(
                "SELECT * FROM tasks WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            
            if not task:
                conn.execute("COMMIT;")
                return None
                
            task_dict = dict(task)
            conn.execute(
                "UPDATE tasks SET status = 'processing', attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (task_dict["id"],)
            )
            conn.execute("COMMIT;")
            return task_dict
        except Exception as e:
            conn.execute("ROLLBACK;")
            logger.error(f"Error locking next task: {e}")
            return None

def update_task(task_id: int, status: str, document_id: str = None, error_message: str = None, progress_info: str = None):
    """Updates task outcome status and links it to document_id if completed."""
    with db.get_connection() as conn:
        if progress_info is not None:
            conn.execute(
                """UPDATE tasks 
                   SET status = ?, document_id = ?, error_message = ?, progress_info = ?, updated_at = CURRENT_TIMESTAMP 
                   WHERE id = ?""",
                (status, document_id, error_message, progress_info, task_id)
            )
        else:
            conn.execute(
                """UPDATE tasks 
                   SET status = ?, document_id = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP 
                   WHERE id = ?""",
                (status, document_id, error_message, task_id)
            )

def update_task_progress(task_id: int, progress_info: str):
    """Updates task progress details while processing."""
    with db.get_connection() as conn:
        conn.execute(
            """UPDATE tasks 
               SET progress_info = ?, updated_at = CURRENT_TIMESTAMP 
               WHERE id = ?""",
            (progress_info, task_id)
        )

def update_progress_json(task_id: int, phase: str, detail: str, progress: int):
    import json
    info = json.dumps({
        "phase": phase,
        "detail": detail,
        "progress": progress
    })
    update_task_progress(task_id, info)

def list_tasks():
    """Lists all tasks in the queue with status and error messages."""
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

def reset_stuck_tasks():
    """Resets stuck 'processing' tasks back to 'pending' (e.g. after a crash), cleaning up partial documents."""
    with db.get_connection() as conn:
        # Clean up partial documents created during mid-ingestion crash
        stuck_tasks = conn.execute("SELECT id, document_id FROM tasks WHERE status = 'processing' AND document_id IS NOT NULL").fetchall()
        for t in stuck_tasks:
            doc_id = t["document_id"]
            if doc_id:
                try:
                    # db.delete_document uses its own connection, so we do it outside the transaction if needed, 
                    # but here we are just in a with block so it will create a separate connection inside delete_document.
                    db.delete_document(doc_id)
                except Exception as e:
                    logger.error(f"Failed to clean up partial document {doc_id} for stuck task {t['id']}: {e}")
                    
        import json
        reset_info = json.dumps({"phase": "Pending", "detail": "Reset after restart", "progress": 0})
        cursor = conn.execute(
            "UPDATE tasks SET status = 'pending', document_id = NULL, progress_info = ? WHERE status = 'processing'",
            (reset_info,)
        )
        return cursor.rowcount

def process_single_task(task: dict) -> bool:
    """Executes a single ingestion task pipeline."""
    task_id = task["id"]
    file_path_str = task["file_path"]
    file_path = Path(file_path_str)
    
    logger.info(f"Ingesting task={task_id} file={file_path.name}")
    try:
        config = get_config()
        
        # 1. Parse PDF
        update_progress_json(task_id, "Parsing PDF", "Initializing extraction...", 0)
        
        def parse_progress(current_page, total_pages):
            pct = int(10 * current_page / max(1, total_pages))
            update_progress_json(task_id, "Parsing PDF", f"Extracting text from page {current_page} of {total_pages}", pct)
            
        parsed = parser.parse_pdf(file_path, progress_callback=parse_progress)
        sha256 = parsed["sha256"]
        filename = parsed["filename"]
        size_bytes = parsed["size_bytes"]
        pages = parsed["pages"]
        
        # 2. Check if already processed (diff-aware skip)
        existing_doc = db.get_document_by_sha256(sha256)
        if existing_doc:
            logger.info(f"Document already ingested (sha256 matched). Skipping LLM processing. doc_id={existing_doc['id']}")
            import json
            skip_info = json.dumps({"phase": "Completed", "detail": "Skipped (Already Ingested)", "progress": 100})
            update_task(task_id, "completed", document_id=existing_doc["id"], progress_info=skip_info)
            return True
            
        # 3. Create document record
        doc_id = str(uuid.uuid4())
        db.add_document(doc_id, filename, sha256, size_bytes)
        update_task(task_id, "processing", document_id=doc_id)
        
        # 4. Chunk document
        update_progress_json(task_id, "Chunking", "Generating text segments...", 10)
        chunks = chunker.chunk_document(pages, config.chunk_size, config.chunk_overlap)
        logger.info(f"Split {filename} into {len(chunks)} chunks. Running enrichment...")
        
        # 5. Enrich chunks
        for idx, chunk in enumerate(chunks):
            chunk_id = chunk["chunk_id"]
            text_content = chunk["text_content"]
            page_start = chunk["page_start"]
            page_end = chunk["page_end"]
            
            pct = 10 + int(85 * (idx + 1) / max(1, len(chunks)))
            update_progress_json(task_id, "Enriching", f"Processing chunk {idx+1}/{len(chunks)} (pages {page_start}-{page_end})", pct)
            logger.info(f"Enriching chunk {idx+1}/{len(chunks)} (pages {page_start}-{page_end})")
            
            # Call Gemini/Nvidia API for summaries, entities, and embeddings
            analysis = enricher.enrich_chunk(text_content)
            
            # Store chunk
            db.add_chunk(
                chunk_id=chunk_id,
                document_id=doc_id,
                page_start=page_start,
                page_end=page_end,
                text_content=text_content,
                summary=analysis["summary"],
                embedding=analysis["embedding"],
                embedding_model=analysis.get("embedding_model")
            )
            
            # Store keywords
            for kw in analysis["keywords"]:
                db.add_keyword(chunk_id, kw.strip().lower())
                
            # Store entities & links
            for ent in analysis["entities"]:
                ent_name = ent["name"]
                ent_type = ent["type"]
                # Create entity ID by lowercasing and sanitizing
                ent_id = f"{ent_type}:{ent_name.strip().lower()}"
                
                db.add_entity(ent_id, ent_name, ent_type)
                db.link_chunk_to_entity(chunk_id, ent_id)
        
        import json
        comp_info = json.dumps({"phase": "Completed", "detail": "Document successfully ingested", "progress": 100})
        update_task(task_id, "completed", document_id=doc_id, progress_info=comp_info)
        logger.info(f"Successfully completed ingestion for task={task_id}")
        return True
        
    except Exception as e:
        logger.exception(f"Failed task={task_id} ingestion: {e}")
        # Clean up partial document if it was created
        if 'doc_id' in locals() and doc_id:
            try:
                db.delete_document(doc_id)
            except Exception as cleanup_err:
                logger.error(f"Failed to cleanup partial document {doc_id}: {cleanup_err}")
        import json
        fail_info = json.dumps({"phase": "Failed", "detail": str(e), "progress": 0})
        update_task(task_id, "failed", document_id=None, error_message=str(e), progress_info=fail_info)
        return False


def run_worker_loop(interval_sec: float = 2.0, max_runs: int = None):
    """
    Runs a worker loop polling for new tasks.
    Used by CLI or server daemon for processing queue.
    """
    logger.info("Starting OpenPDFSpecs background worker loop...")
    reset_stuck_tasks()
    
    run_count = 0
    while True:
        task = get_next_task()
        if task:
            process_single_task(task)
            run_count += 1
            if max_runs and run_count >= max_runs:
                break
        else:
            time.sleep(interval_sec)
