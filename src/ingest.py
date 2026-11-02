"""
Multi-tenant ingestion worker — TWO STAGES (REVAMP Phase E).

Stage 1 (`process_task`) — download → parse → diff-skip → chunk → embed → store.
    Makes NO network calls: embeddings are local since Phase C. The document is fully
    searchable, lexically and semantically, the moment this finishes.

Stage 2 (`run_enrichment_pass`) — LLM summaries + entities for chunks whose `enriched_at`
    is NULL. Rate-limited, resumable, and entirely off the critical path.

Previously these were one pass and nothing was written until every chunk had been
enriched: ~13,500 chunks at a 1.5s throttle meant ~5.6 hours before anything was
searchable, and any failure discarded the whole document.
"""
import os
import json
import socket
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


def worker_id() -> str:
    """
    Identity of this worker process, stamped on every task it claims.

    Crash recovery uses it to tell this worker's abandoned tasks from another worker's
    live ones (pgdb.reset_stuck_tasks, migration 0008). host+pid is enough: a restarted
    process gets a new pid, so it never mistakes a live sibling's task for its own.
    """
    return f"{socket.gethostname()}:{os.getpid()}"

# Both of these go through Config, which already applies the PDFSPECS_* env override and
# the max(1, ...) floor. Reading os.getenv directly here meant the matching config.json
# keys were never read at all, and the two sources disagreed on the default.
def _enrich_concurrency() -> int:
    """Parallel enrichment calls (config.json `enrich_concurrency` / PDFSPECS_ENRICH_CONCURRENCY)."""
    return get_config().enrich_concurrency


def _max_attempts() -> int:
    """Attempts before a task is marked failed (config.json / PDFSPECS_MAX_TASK_ATTEMPTS)."""
    return get_config().max_task_attempts


def _embed_chunk_batch() -> int:
    """Chunks per local embedding batch (memory/throughput trade-off)."""
    return max(1, int(os.getenv("PDFSPECS_EMBED_CHUNK_BATCH", "32")))


def _org_provider_settings(org_id: str) -> enricher.ProviderSettings:
    """Build a per-org NVIDIA config from that org's saved settings (BYO key)."""
    return enricher.ProviderSettings(
        nvidia_api_key=pgdb.get_setting(org_id, "nvidia_api_key", decrypt=True),
        llm_model=pgdb.get_setting(org_id, "llm_model") or enricher.NVIDIA_LLM_MODEL,
        # The embedding model's dimension must equal the vector column (pgdb.EMBEDDING_DIM)
        # or add_chunk raises EmbeddingDimError and the task fails loudly.
        # Resolved from PDFSPECS_EMBEDDING_BACKEND; both options are 1024-d, matching
        # the vector column, so switching backends needs no migration.
        embedding_model=enricher.default_embedding_model(),
    )


def _progress(task_id, phase: str, detail: str, progress: int):
    pgdb.update_task_progress(task_id, json.dumps(
        {"phase": phase, "detail": detail, "progress": progress}))


def _embed_chunks(chunks: list, settings, task_id, usage_sink=None) -> list:
    """
    Embed every chunk in batches, returning a list of window-vector lists.

    Batched across chunks so the backend fills its batches; embedding per chunk meant
    hundreds of tiny, under-filled calls per document.
    """
    n = len(chunks)
    texts = [c["text_content"] for c in chunks]
    out = []
    step = _embed_chunk_batch()
    for start in range(0, n, step):
        batch = texts[start:start + step]
        out.extend(enricher.embed_texts_batch(
            batch, settings=settings, input_type="passage", usage_sink=usage_sink))
        done = min(start + step, n)
        _progress(task_id, "Embedding", f"Embedded {done}/{n} chunks",
                  20 + int(70 * done / max(1, n)))
    return out


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

        # 6. Embed, batched. Offline with the `local` backend; batched API calls with
        #    `nvidia`. Either way this is the only work between chunking and searchability.
        def usage_sink(purpose, prompt_tokens, completion_tokens):
            pgdb.record_token_usage(org_id, purpose, prompt_tokens, completion_tokens)

        embeddings = _embed_chunks(chunks, settings, task_id, usage_sink=usage_sink)

        # 7. Store chunks + window vectors in one transaction per document.
        #    `summary` is left NULL: stage 2 fills it in and sets enriched_at.
        rows = [{
            "page_start": c["page_start"],
            "page_end": c["page_end"],
            "text_content": c["text_content"],
            "summary": None,
            "embeddings": embeddings[i] if i < len(embeddings) else [],
            "embedding_model": settings.resolved_embedding_model(),
        } for i, c in enumerate(chunks)]

        _progress(task_id, "Storing", f"Writing {len(rows)} chunks", 92)
        pgdb.add_chunks_bulk(org_id, doc_id, rows)

        pgdb.update_task(task_id, "completed", document_id=doc_id,
                         progress_info=json.dumps({"phase": "Completed",
                             "detail": f"Searchable: {len(rows)} chunks stored. "
                                       f"Summaries/entities pending enrichment.",
                             "progress": 100}))
        logger.info("Completed task=%s (%d chunks stored, enrichment pending)",
                    task_id, len(rows))
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


# =====================================================================
# Stage 2 — enrichment (LLM summaries + entities), off the critical path
# =====================================================================
def run_enrichment_pass(org_id: str, limit: int = None, progress_cb=None) -> int:
    """
    Enrich pending chunks for one org. Returns how many were enriched.

    Resumable by construction: each chunk is marked `enriched_at` as it completes, so an
    interrupted run simply resumes where it stopped. A chunk whose LLM call keeps failing
    is marked done anyway (with no summary) rather than blocking the queue forever — it
    stays searchable via its text and embeddings either way.
    """
    settings = _org_provider_settings(org_id)
    if not settings.nvidia_api_key:
        raise enricher.APIKeyMissingError(
            "No NVIDIA API key configured for this org — cannot enrich.")

    def usage_sink(purpose, prompt_tokens, completion_tokens):
        pgdb.record_token_usage(org_id, purpose, prompt_tokens, completion_tokens)

    done = 0
    batch_size = 50
    while True:
        remaining = None if limit is None else max(0, limit - done)
        if remaining == 0:
            break
        batch = pgdb.chunks_pending_enrichment(
            org_id, limit=batch_size if remaining is None else min(batch_size, remaining))
        if not batch:
            break

        def work(group):
            """Enrich a group of chunks in ONE LLM call, falling back per-chunk on trouble."""
            try:
                metas = enricher.extract_metadata_batch(
                    [c["text_content"] for c in group], settings, usage_sink=usage_sink)
                for chunk, meta in zip(group, metas):
                    pgdb.save_enrichment(org_id, chunk["id"], meta["summary"], meta["entities"])
                return True
            except enricher.APIKeyMissingError:
                raise
            except Exception as e:
                # A batch that can't be reconciled (dropped/reordered ids, unparseable JSON)
                # must not lose the whole group — retry its chunks individually so one bad
                # response costs accuracy on at most one chunk, never a mismatched summary.
                logger.warning("Batch enrichment failed for %d chunks (%s: %s); retrying "
                               "individually.", len(group), type(e).__name__, e)
                for chunk in group:
                    try:
                        meta = enricher.extract_metadata(
                            chunk["text_content"], settings, usage_sink=usage_sink)
                        pgdb.save_enrichment(org_id, chunk["id"],
                                             meta["summary"], meta["entities"])
                    except enricher.APIKeyMissingError:
                        raise
                    except Exception as e2:
                        logger.warning("Enrichment failed for chunk %s (%s: %s); marking "
                                       "done without a summary.",
                                       chunk["id"], type(e2).__name__, e2)
                        pgdb.mark_enrichment_failed(org_id, chunk["id"])
                return False

        # Split the fetched chunks into LLM-sized groups, then run those groups in parallel.
        size = enricher.ENRICH_BATCH_SIZE
        groups = [batch[i:i + size] for i in range(0, len(batch), size)]

        with ThreadPoolExecutor(max_workers=_enrich_concurrency()) as pool:
            futures = {pool.submit(work, g): len(g) for g in groups}
            for fut in as_completed(futures):
                fut.result()
                done += futures[fut]
                if progress_cb:
                    progress_cb(done)

    return done


def run_worker_loop(interval_sec: float = 2.0, max_runs: int = None, enrich: bool = True):
    """
    Poll the queue and process tasks (stage 1). When the queue is idle, spend the time on
    stage-2 enrichment so summaries backfill without a second process.
    """
    import time
    me = worker_id()
    logger.info("Starting ingestion worker %s (enrichment %s)...",
                me, "enabled" if enrich else "disabled")
    # Scoped to this worker's own abandoned tasks plus any whose lease has expired, so
    # starting a second worker no longer requeues (and deletes the document of) a task
    # the first one is actively processing.
    pgdb.reset_stuck_tasks(worker_id=me)
    run_count = 0
    while True:
        task = pgdb.get_next_task(worker_id=me)
        if task:
            process_task(task)
            run_count += 1
            if max_runs and run_count >= max_runs:
                break
            continue

        # Queue empty. Backfill enrichment in small slices so a newly queued document is
        # picked up promptly rather than waiting for a whole corpus to be enriched.
        if enrich:
            # Per-org error handling, INSIDE the loop. With the handler outside it, the
            # first org lacking an NVIDIA key aborted the whole scan — and since
            # orgs_with_pending_enrichment() is a DISTINCT with no ORDER BY, that starved
            # every other tenant intermittently, depending on row order.
            try:
                orgs = pgdb.orgs_with_pending_enrichment()
            except Exception as e:
                logger.error("Could not list orgs pending enrichment: %s", e)
                orgs = []

            worked = False
            for org in orgs:
                try:
                    if run_enrichment_pass(org, limit=25):
                        worked = True
                        break
                except enricher.APIKeyMissingError:
                    continue               # this org has no key; others may
                except Exception as e:
                    logger.error("Enrichment pass failed for org %s: %s", org, e)
            if not worked:
                time.sleep(interval_sec)
        else:
            time.sleep(interval_sec)
