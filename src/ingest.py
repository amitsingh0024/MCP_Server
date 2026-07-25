"""
Multi-tenant ingestion worker.

Dequeues tasks from Postgres, pulls the PDF from Supabase Storage, parses/chunks it,
enriches each chunk with the OWNING ORG's provider key (BYO key), and writes everything
back org-scoped. Replaces the SQLite-based src/queue.py for the hosted service.

Pipeline per task:  download → parse → (diff-skip) → chunk → enrich (parallel) → store.
"""
import os
import json
import shutil
import logging
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import src.pgdb as pgdb
import src.storage as storage
import src.parser as parser
import src.chunker as chunker
import src.enricher as enricher
from src.config import get_config

logger = logging.getLogger("pdfspecs.ingest")

# Fixed platform embedding model/dim (must match chunks.embedding vector(768)).
EMBEDDING_MODEL = os.getenv("PDFSPECS_EMBEDDING_MODEL", "text-embedding-004")


def _enrich_concurrency() -> int:
    return max(1, int(os.getenv("PDFSPECS_ENRICH_CONCURRENCY", "4")))


def _max_attempts() -> int:
    return max(1, int(os.getenv("PDFSPECS_MAX_TASK_ATTEMPTS", "3")))


def _org_provider_settings(org_id: str) -> enricher.ProviderSettings:
    """Build a per-org provider config from that org's saved settings (BYO key)."""
    provider = pgdb.get_setting(org_id, "provider") or "gemini"
    llm_model = pgdb.get_setting(org_id, "llm_model") or (
        "gemini-2.5-flash" if provider == "gemini" else "meta/llama-3.1-8b-instruct"
    )
    return enricher.ProviderSettings(
        provider=provider,
        gemini_api_key=pgdb.get_setting(org_id, "gemini_api_key", decrypt=True),
        nvidia_api_key=pgdb.get_setting(org_id, "nvidia_api_key", decrypt=True),
        llm_model=llm_model,
        # Embedding model is fixed platform-wide so every vector is 768-d.
        embedding_model=EMBEDDING_MODEL,
    )


def _progress(task_id, phase: str, detail: str, progress: int):
    pgdb.update_task_progress(task_id, json.dumps(
        {"phase": phase, "detail": detail, "progress": progress}))


def _enrich_all(chunks: list, settings, usage_sink, task_id) -> list:
    """Enrich chunks in parallel (I/O-bound API calls); return [(chunk, analysis), ...]."""
    n = len(chunks)
    results = [None] * n
    done = 0

    def work(i, chunk):
        return i, chunk, enricher.enrich_chunk(
            chunk["text_content"], settings=settings, usage_sink=usage_sink)

    with ThreadPoolExecutor(max_workers=_enrich_concurrency()) as pool:
        futures = [pool.submit(work, i, c) for i, c in enumerate(chunks)]
        for fut in as_completed(futures):
            i, chunk, analysis = fut.result()
            results[i] = (chunk, analysis)
            done += 1
            pct = 10 + int(85 * done / max(1, n))
            _progress(task_id, "Enriching", f"Processed chunk {done}/{n}", pct)
    return [r for r in results if r is not None]


def process_task(task: dict) -> bool:
    """Run one ingestion task end-to-end. Returns True on success."""
    task_id = task["id"]
    org_id = str(task["org_id"])
    storage_key = task["storage_key"]
    filename = task.get("original_filename") or Path(storage_key).name
    attempts_made = int(task.get("attempts", 0)) + 1  # get_next_task already incremented

    logger.info("Ingesting task=%s org=%s file=%s", task_id, org_id, filename)
    doc_id = None
    tmpdir = None
    try:
        settings = _org_provider_settings(org_id)

        # 1. Download from storage to a temp file.
        _progress(task_id, "Downloading", "Fetching file from storage", 0)
        tmpdir = tempfile.mkdtemp(prefix="pdfspecs-")
        local_path = Path(tmpdir) / storage.safe_filename(filename)
        storage.download_to_path(storage_key, local_path)

        # 2. Parse.
        def parse_cb(cur, total):
            _progress(task_id, "Parsing PDF",
                      f"Extracting page {cur} of {total}", int(10 * cur / max(1, total)))

        parsed = parser.parse_pdf(local_path, progress_callback=parse_cb)
        sha256 = parsed["sha256"]

        # 3. Diff-aware skip (per org).
        existing = pgdb.get_document_by_sha256(org_id, sha256)
        if existing:
            logger.info("Doc already ingested for org (sha match); skipping. doc=%s", existing["id"])
            pgdb.update_task(task_id, "completed", document_id=str(existing["id"]),
                             progress_info=json.dumps({"phase": "Completed",
                                 "detail": "Skipped (already ingested)", "progress": 100}))
            return True

        # 4. Create the document row.
        doc_id = pgdb.add_document(org_id, filename, sha256, parsed["size_bytes"])
        pgdb.update_task(task_id, "processing", document_id=doc_id)

        # 5. Chunk (chunk size/overlap are platform settings).
        cfg = get_config()
        chunks = chunker.chunk_document(parsed["pages"], cfg.chunk_size, cfg.chunk_overlap)
        logger.info("Split %s into %d chunks", filename, len(chunks))
        _progress(task_id, "Chunking", f"{len(chunks)} segments", 10)

        # 6. Enrich (parallel) using the org's key; usage recorded org-scoped.
        def usage_sink(purpose, prompt_tokens, completion_tokens):
            pgdb.record_token_usage(org_id, purpose, prompt_tokens, completion_tokens)

        enriched = _enrich_all(chunks, settings, usage_sink, task_id)

        # 7. Store chunks, keywords, entities (all org-scoped).
        for chunk, analysis in enriched:
            chunk_id = pgdb.add_chunk(
                org_id, doc_id, chunk["page_start"], chunk["page_end"],
                chunk["text_content"], analysis.get("summary", ""),
                analysis.get("embedding"), analysis.get("embedding_model"))
            for kw in analysis.get("keywords", []):
                if kw and kw.strip():
                    pgdb.add_keyword(org_id, chunk_id, kw.strip().lower())
            for ent in analysis.get("entities", []):
                name, etype = ent.get("name", "").strip(), ent.get("type", "concept")
                if not name:
                    continue
                ent_id = f"{etype}:{name.lower()}"
                pgdb.add_entity(org_id, ent_id, name, etype)
                pgdb.link_chunk_to_entity(org_id, chunk_id, ent_id)

        pgdb.update_task(task_id, "completed", document_id=doc_id,
                         progress_info=json.dumps({"phase": "Completed",
                             "detail": "Document successfully ingested", "progress": 100}))
        logger.info("Completed task=%s", task_id)
        return True

    except Exception as e:
        logger.exception("Failed task=%s: %s", task_id, e)
        if doc_id:
            try:
                pgdb.delete_document(org_id, doc_id)
            except Exception as ce:
                logger.error("Cleanup of partial doc %s failed: %s", doc_id, ce)
        # Retry until the attempt cap, then give up (FIXPLAN 3.5).
        if attempts_made >= _max_attempts():
            pgdb.update_task(task_id, "failed", error_message=str(e),
                             progress_info=json.dumps({"phase": "Failed",
                                 "detail": str(e), "progress": 0}))
        else:
            pgdb.update_task(task_id, "pending", error_message=str(e),
                             progress_info=json.dumps({"phase": "Pending",
                                 "detail": f"Retry after error (attempt {attempts_made})",
                                 "progress": 0}))
        return False
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def run_worker_loop(interval_sec: float = 2.0, max_runs: int = None):
    """Poll the queue and process tasks. Used by the hosted worker process."""
    import time
    logger.info("Starting multi-tenant ingestion worker...")
    pgdb.reset_stuck_tasks()
    run_count = 0
    while True:
        task = pgdb.get_next_task()
        if task:
            process_task(task)
            run_count += 1
            if max_runs and run_count >= max_runs:
                break
        else:
            time.sleep(interval_sec)
