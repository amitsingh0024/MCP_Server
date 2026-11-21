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
import time
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


def _enrich_slice() -> int:
    """
    Chunks per idle-loop enrichment slice (PDFSPECS_ENRICH_SLICE).

    Kept small by default so a document queued while the loop is enriching waits one
    slice, not a whole corpus. Raise it once the queue is drained: the per-slice
    overhead (two org-scan queries and a pool teardown) then buys nothing.
    """
    return max(1, int(os.getenv("PDFSPECS_ENRICH_SLICE", "25")))


class _StageBackoff:
    """
    Per-org cooldown for an idle-loop stage whose provider is failing.

    Both idle stages call a provider that can fail for minutes or hours at a stretch,
    and both failed expensively before this existed:

    * The embedding backfill does not fail fast. `_post_with_retry` spends 5 attempts at
      a 45s timeout, so ~250s of every loop pass went into a call that could not succeed
      — and the backfill runs FIRST, so that time came out of enrichment. Measured
      against nemotron-3.5-embed-1b answering `504 service request timed out after 120s`
      to every single-input request while every other model on the same key answered in
      under a second.
    * Enrichment answered a rate limit by multiplying its own request volume. See
      enricher.RateLimitedError: 45 requests to enrich 8 chunks, sent while the provider
      was already refusing.

    Holding the org off hands the time to whatever still works and, for a rate limit,
    stops the requests that caused it. Doubling per consecutive failure probes a long
    outage proportionally less often. Per org, so one tenant's dead model or exhausted
    quota cannot stall another's.
    """

    def __init__(self, name: str, start_sec: float, max_sec: float):
        self.name = name
        self.start_sec = start_sec
        self.max_sec = max_sec
        self._entries: dict = {}

    def active(self, org_id: str) -> bool:
        """True while this org is in a post-failure cooldown for this stage."""
        entry = self._entries.get(org_id)
        return bool(entry) and time.monotonic() < entry["until"]

    def note_failure(self, org_id: str, *, after_progress: bool = False) -> float:
        """
        Start or extend this org's cooldown. Returns the delay applied, in seconds.

        Escalation is for a stage that cannot get anywhere. `after_progress=True` says
        the pass did real work and only then met the provider — a rate limit reached
        partway through a slice. That earns the floor delay and resets the escalation,
        because the stage is working and the limit is being paced, not waited out.

        Without that distinction this was a one-way ratchet. A slice of 200 chunks
        enriched 58-190 of them and then took a 429, which raises rather than returns,
        so the pass never reached clear() and every slice counted as a failure:

            06:31:34  stood down  126s      <- the one slice that returned cleanly
            06:35:45  stood down  245s
            06:42:14  stood down  584s
            06:55:27  stood down  979s
            07:14:00  stood down 1809s      <- the ceiling, and it stayed there

        That is 120 -> 240 -> 480 -> 960 -> 1800 while the stage was summarising ~100
        chunks per burst throughout. Measured over those three hours: 45 minutes of work
        to 133 minutes of standing down, with a single probe on the same key answering
        HTTP 200 in 0.55s three minutes into a standdown. The limit is short-lived; only
        the penalty for it was long.
        """
        if after_progress:
            # Floor, and forget the escalation: `delay` 0.0 makes the next failure with
            # no progress start from start_sec rather than doubling off this one.
            self._entries[org_id] = {"until": time.monotonic() + self.start_sec,
                                     "delay": 0.0}
            return self.start_sec
        previous = self._entries.get(org_id, {}).get("delay", 0.0)
        delay = min(self.max_sec, previous * 2 if previous else self.start_sec)
        self._entries[org_id] = {"until": time.monotonic() + delay, "delay": delay}
        return delay

    def clear(self, org_id: str) -> None:
        """Forget this org's cooldown after a pass that got somewhere."""
        self._entries.pop(org_id, None)

    def report(self) -> dict | None:
        """
        {orgs, resumes_in_sec} for /health, or None when nothing is held off.

        `escalated_to_sec` appears only while the delay is doubling, i.e. consecutive
        passes that got nowhere. A cooldown imposed after a pass that did work carries
        no such key, and the difference matters to whoever is reading: the first means
        the stage cannot proceed, the second means it is pacing itself and progress is
        still being made. Reported as one or the other, a ratchet to the ceiling read
        as intended behaviour for three hours.

        Counts only. Org ids do not belong in an unauthenticated endpoint, and the point
        of the field is that a held-off stage is otherwise invisible: the only symptom is
        a pending count that never moves.
        """
        now = time.monotonic()
        held = [e for e in list(self._entries.values()) if e["until"] > now]
        if not held:
            return None
        longest = max(held, key=lambda e: e["until"])
        out = {"orgs": len(held), "resumes_in_sec": round(longest["until"] - now, 1)}
        if longest.get("delay"):
            out["escalated_to_sec"] = round(longest["delay"], 1)
        return out


# A dead endpoint stays dead for a while, so start long. A rate limit clears fast, so
# start short: measured, NVIDIA accepted a request 0.55s into what had been a standdown,
# and the productive bursts between standdowns ran 1-5 minutes. 30s is the pause after a
# slice that worked and then hit the limit, so the duty cycle stays high; the ceiling is
# only ever reached by consecutive passes that achieve nothing, and 600s there probes a
# genuine outage 6 times an hour instead of twice.
_embed_backoff = _StageBackoff(
    "embed",
    float(os.getenv("PDFSPECS_EMBED_BACKOFF_SEC", "300")),
    float(os.getenv("PDFSPECS_EMBED_BACKOFF_MAX_SEC", "3600")))
_enrich_backoff = _StageBackoff(
    "enrich",
    float(os.getenv("PDFSPECS_ENRICH_BACKOFF_SEC", "30")),
    float(os.getenv("PDFSPECS_ENRICH_BACKOFF_MAX_SEC", "600")))


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
    # Also beats the worker heartbeat. A single book can occupy the loop for an hour, so
    # a heartbeat updated only once per loop iteration would read as dead for most of a
    # healthy run; this fires per page and per embedding batch.
    _worker_state["heartbeat"] = time.time()
    _worker_state["task_id"] = task_id
    pgdb.update_task_progress(task_id, json.dumps(
        {"phase": phase, "detail": detail, "progress": progress}))


def _embed_chunks(chunks: list, settings, task_id, usage_sink=None) -> list:
    """
    Embed every chunk in batches.

    Returns (per-chunk window-vector lists, count of chunks left unembedded). A batch the
    provider could not embed yields empty lists rather than raising, so one outage costs
    those chunks' vectors instead of the entire document.

    Batched across chunks so the backend fills its batches; embedding per chunk meant
    hundreds of tiny, under-filled calls per document.
    """
    n = len(chunks)
    texts = [c["text_content"] for c in chunks]
    out = []
    unembedded = 0
    step = _embed_chunk_batch()
    for start in range(0, n, step):
        batch = texts[start:start + step]
        try:
            out.extend(enricher.embed_texts_batch(
                batch, settings=settings, input_type="passage", usage_sink=usage_sink))
        except Exception as e:
            # A provider failure here used to abort the task, discarding the parse — which
            # on this corpus is up to nine hours of OCR thrown away for one transient 5xx.
            # The chunks are stored text-only instead (searchable lexically) and the
            # embedding backfill completes them later.
            logger.error("Embedding failed for chunks %d-%d (%s: %s); storing them "
                         "text-only for the backfill pass.",
                         start, min(start + step, n) - 1, type(e).__name__, e)
            out.extend([[] for _ in batch])
            unembedded += len(batch)
        done = min(start + step, n)
        _progress(task_id, "Embedding", f"Embedded {done}/{n} chunks",
                  20 + int(70 * done / max(1, n)))
    return out, unembedded


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

        # 2. Parse. A markdown source skips the text-layer/OCR path entirely: these books
        #    exist as both, and the PDFs were printed from the markdown by a converter
        #    that mangles Devanagari on the way out (see parser.parse_markdown). Reading
        #    the markdown is ~0.7s for the whole corpus against ~131 hours of OCR.
        is_markdown = local_path.suffix.lower() in (".md", ".markdown")
        phase = "Parsing" if is_markdown else "Parsing PDF"
        unit = "record" if is_markdown else "page"

        def parse_cb(cur, total):
            _progress(task_id, phase,
                      f"Extracting {unit} {cur} of {total}", int(10 * cur / max(1, total)))

        parsed = (parser.parse_markdown(local_path, progress_callback=parse_cb)
                  if is_markdown
                  else parser.parse_pdf(local_path, progress_callback=parse_cb))
        sha256 = parsed["sha256"]

        # 3. Diff-aware skip (per org) — but only for a document that actually holds
        #    chunks. The row below is created BEFORE chunking, so an ingest killed in
        #    between (container restart, OOM, lost lease) leaves an empty one. Keying the
        #    skip on mere existence meant the retry reported "already ingested" over a
        #    book with nothing in it, permanently, and the only symptom was a search that
        #    came up short. An empty row is abandoned work: drop it and ingest properly.
        existing = pgdb.get_document_by_sha256(org_id, sha256)
        if existing:
            n_chunks = pgdb.document_chunk_count(org_id, str(existing["id"]))
            if n_chunks:
                logger.info("Doc already ingested for org (sha match, %d chunks); "
                            "skipping. doc=%s", n_chunks, existing["id"])
                pgdb.update_task(task_id, "completed", document_id=str(existing["id"]),
                                 progress_info=json.dumps({"phase": "Completed",
                                     "detail": "Skipped (already ingested)", "progress": 100}))
                return True
            logger.warning("Found an empty document row for %s (doc=%s) left by an "
                           "abandoned ingest; deleting it and re-ingesting.",
                           filename, existing["id"])
            pgdb.delete_document(org_id, str(existing["id"]))

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

        embeddings, unembedded = _embed_chunks(chunks, settings, task_id,
                                               usage_sink=usage_sink)

        # 7. Store chunks + window vectors in one transaction per document.
        #    `summary` is left NULL: stage 2 fills it in and sets enriched_at.
        #    embedding_model is recorded ONLY for chunks that actually have vectors: it is
        #    what corpus_embedding_models() reports, and a model claimed over chunks with
        #    no vectors would corrupt search's query/corpus match check.
        rows = []
        for i, c in enumerate(chunks):
            vecs = embeddings[i] if i < len(embeddings) else []
            rows.append({
                "page_start": c["page_start"],
                "page_end": c["page_end"],
                "text_content": c["text_content"],
                "summary": None,
                "embeddings": vecs,
                "embedding_model": settings.resolved_embedding_model() if vecs else None,
            })

        _progress(task_id, "Storing", f"Writing {len(rows)} chunks", 92)
        pgdb.add_chunks_bulk(org_id, doc_id, rows)

        detail = (f"Searchable: {len(rows)} chunks stored. "
                  f"Summaries/entities pending enrichment.")
        if unembedded:
            detail += (f" {unembedded} chunk(s) await embedding backfill "
                       f"(provider was unavailable).")
        pgdb.update_task(task_id, "completed", document_id=doc_id,
                         progress_info=json.dumps({"phase": "Completed",
                             "detail": detail, "progress": 100}))
        logger.info("Completed task=%s (%d chunks stored, %d awaiting embeddings, "
                    "enrichment pending)", task_id, len(rows), unembedded)
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
# Stage 1.5 — embedding backfill for chunks stored text-only
# =====================================================================
def run_embedding_pass(org_id: str, limit: int = None, progress_cb=None) -> int:
    """
    Give vectors to chunks that were stored without them. Returns how many were embedded.

    Ingestion no longer throws a document away when the embedding provider is down, so
    those chunks land searchable-but-lexical-only and are completed here. Resumable by
    construction: a chunk leaves the queue the moment its vectors are committed, so an
    interrupted pass simply resumes.

    Unlike enrichment this needs no LLM, so it still completes for an org whose provider
    offers embeddings but no chat model.
    """
    settings = _org_provider_settings(org_id)
    model = settings.resolved_embedding_model()

    def usage_sink(purpose, prompt_tokens, completion_tokens):
        pgdb.record_token_usage(org_id, purpose, prompt_tokens, completion_tokens)

    done = 0
    batch_size = _embed_chunk_batch()
    # A chunk whose text yields no embedding windows at all (blank or whitespace-only)
    # can never leave the pending set, and the query returns oldest-first — so without
    # this the pass would re-fetch the same unembeddable row forever.
    attempted = set()
    while True:
        remaining = None if limit is None else max(0, limit - done)
        if remaining == 0:
            break
        batch = pgdb.chunks_pending_embedding(
            org_id, limit=batch_size if remaining is None else min(batch_size, remaining))
        if not batch:
            break
        if all(c["id"] in attempted for c in batch):
            logger.warning("Embedding backfill: %d chunk(s) yielded no vectors and cannot "
                           "be embedded (empty text?); leaving them lexical-only.",
                           len(batch))
            break
        attempted.update(c["id"] for c in batch)

        vectors = enricher.embed_texts_batch(
            [c["text_content"] for c in batch], settings=settings,
            input_type="passage", usage_sink=usage_sink)
        for chunk, vecs in zip(batch, vectors):
            if vecs:
                pgdb.save_chunk_embeddings(org_id, chunk["id"], vecs, model)
                done += 1
                if progress_cb:
                    progress_cb(done)
    return done


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
            except enricher.RateLimitedError:
                # The ONE failure the per-chunk fallback must not take. It would turn one
                # rejected batch into 8 more calls at 5 attempts each — 45 requests where
                # the happy path needs 1 — sent while the provider is already refusing,
                # and then mark each of those 8 chunks permanently done with no summary.
                # Let it out; the worker loop stands the whole org down instead.
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
                    except (enricher.APIKeyMissingError, enricher.RateLimitedError):
                        # Neither is this chunk's fault, and mark_enrichment_failed is
                        # permanent — chunks_pending_enrichment selects on
                        # `enriched_at IS NULL`, so a chunk marked here is never offered
                        # again. A transient rate limit must not be recorded as a final
                        # outcome; that is how 602 chunks lost their summaries.
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


# ---------------------------------------------------------------------------
# Worker liveness
# ---------------------------------------------------------------------------
# The worker is a daemon thread inside the API process, so uvicorn keeps answering
# after it dies. /health returned a flat {"status": "ok"} through a worker that had
# been dead for hours, while 13 tasks sat in 'pending' and the only visible symptom
# was a queue that never moved. These let /health report the thread, not the web server.
_worker_state = {"started_at": None, "heartbeat": None, "iterations": 0, "task_id": None}

# How often the loop re-sweeps for tasks abandoned by a worker that died mid-book.
# Without this the sweep ran only at startup, so a task whose owner vanished while this
# worker was already running stayed 'processing' forever — observed stranded for an hour
# against a 15-minute lease, and only a container restart freed it.
_SWEEP_INTERVAL_SEC = float(os.getenv("PDFSPECS_STUCK_SWEEP_SEC", "300"))


def worker_state() -> dict:
    """Snapshot of the worker thread's progress, for /health. Never raises."""
    state = dict(_worker_state)
    # Report a held-off stage. Without this the only symptom of a dead embeddings
    # endpoint or a rate-limited key is a pending count that never moves.
    for backoff in (_embed_backoff, _enrich_backoff):
        try:
            report = backoff.report()
        except Exception:
            continue
        if report:
            state[f"{backoff.name}_backoff"] = report
    return state


def run_worker_loop(interval_sec: float = 2.0, max_runs: int = None, enrich: bool = True):
    """
    Poll the queue and process tasks (stage 1). When the queue is idle, spend the time
    backfilling: embeddings first (search-critical, no LLM needed), then stage-2
    enrichment, so both complete without a second process.

    `enrich` governs only the LLM stage; the embedding backfill always runs, since a
    corpus missing vectors is more broken than one missing summaries.
    """
    me = worker_id()
    logger.info("Starting ingestion worker %s (enrichment %s)...",
                me, "enabled" if enrich else "disabled")
    # Scoped to this worker's own abandoned tasks plus any whose lease has expired, so
    # starting a second worker no longer requeues (and deletes the document of) a task
    # the first one is actively processing.
    pgdb.reset_stuck_tasks(worker_id=me)
    _worker_state["started_at"] = time.time()
    _worker_state["heartbeat"] = time.time()
    last_sweep = time.monotonic()
    run_count = 0
    while True:
        _worker_state["heartbeat"] = time.time()
        _worker_state["iterations"] += 1
        # EVERY iteration is wrapped. This loop used to run bare, so one transient error
        # from get_next_task — a pooled connection dropped while the instance idled, say
        # — propagated out of run_worker_loop and ended the daemon thread. Silently: the
        # API kept serving, /health kept returning ok, and the queue simply stopped
        # draining until someone restarted the container. Losing one iteration to a
        # logged error is always better than losing the worker.
        try:
            # Re-sweep abandoned tasks periodically, not just at startup: a worker that
            # dies mid-book leaves its task 'processing' with a lease nothing else
            # reclaims while this worker is already running.
            #
            # Deliberately twice the lease, unlike the startup sweep. Reclaiming a task
            # also DELETES its partial document, so stealing one from a worker that is
            # merely slow destroys live work. The heartbeat is written per embedding
            # batch, and on a CPU-starved instance a 32-chunk batch has been measured at
            # ~10 minutes — close enough to the 15-minute lease to be dangerous when the
            # sweep runs every few minutes instead of once at boot. At startup the
            # ordinary lease is still right: nothing of ours is in flight yet.
            if time.monotonic() - last_sweep >= _SWEEP_INTERVAL_SEC:
                last_sweep = time.monotonic()
                freed = pgdb.reset_stuck_tasks(
                    worker_id=me, lease_minutes=pgdb.TASK_LEASE_MINUTES * 2)
                if freed:
                    logger.warning("Requeued %d task(s) abandoned by a dead worker.", freed)

            task = pgdb.get_next_task(worker_id=me)
            if task:
                process_task(task)
                run_count += 1
                _worker_state["task_id"] = None
                if max_runs and run_count >= max_runs:
                    break
                continue

            # Queue empty. Backfill embeddings FIRST: they are the search-critical half, they
            # need no LLM, and a corpus missing vectors is more broken than one missing
            # summaries. Runs regardless of `enrich`, which governs only the LLM stage.
            worked = False
            try:
                orgs = pgdb.orgs_with_pending_embedding()
            except Exception as e:
                logger.error("Could not list orgs pending embedding: %s", e)
                orgs = []
            for org in orgs:
                if _embed_backoff.active(org):
                    continue
                # run_embedding_pass raises on a provider error, which would otherwise
                # hide the chunks it had already committed — and a pass that embedded 30
                # before timing out on the 31st is a flaky provider, not a dead one, so
                # backing off on it would strand the corpus. progress_cb keeps the count.
                progress = {"n": 0}
                try:
                    embedded = run_embedding_pass(
                        org, limit=200, progress_cb=lambda n: progress.__setitem__("n", n))
                except Exception as e:
                    embedded = progress["n"]
                    if not embedded:
                        delay = _embed_backoff.note_failure(org)
                        logger.error("Embedding backfill failed for org %s with nothing "
                                     "embedded (%s: %s); holding it off for %.0fs so "
                                     "enrichment keeps the loop.",
                                     org, type(e).__name__, e, delay)
                        continue
                    logger.warning("Embedding backfill for org %s embedded %d chunk(s) "
                                   "then failed (%s: %s); treating it as progress.",
                                   org, embedded, type(e).__name__, e)
                if embedded:
                    _embed_backoff.clear(org)
                    worked = True
                    break
                # The org is in this list precisely because it has chunks with no vectors,
                # so a pass that embedded none of them got nowhere — whether the provider
                # returned empty vectors or the chunks are genuinely unembeddable. Either
                # way an immediate retry repeats it, including the API calls it costs.
                delay = _embed_backoff.note_failure(org)
                logger.warning("Embedding backfill embedded nothing for org %s; holding "
                               "it off for %.0fs.", org, delay)
            if worked:
                continue

            # Then enrichment, in small slices so a newly queued document is picked up
            # promptly rather than waiting for a whole corpus to be enriched.
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
                    if _enrich_backoff.active(org):
                        continue
                    # A rate limit can arrive partway through a slice, which would hide
                    # the chunks already summarised. They are committed per chunk, so
                    # count them.
                    progress = {"n": 0}
                    try:
                        if run_enrichment_pass(
                                org, limit=_enrich_slice(),
                                progress_cb=lambda n: progress.__setitem__("n", n)):
                            _enrich_backoff.clear(org)
                            worked = True
                            break
                    except enricher.APIKeyMissingError:
                        continue               # this org has no key; others may
                    except enricher.RateLimitedError as e:
                        # A limit reached partway through a slice is the normal shape of
                        # this: the slice was summarising chunks until it hit. Escalating
                        # on that turned a short limit into 30-minute outages (see
                        # _StageBackoff.note_failure), so progress holds the delay at the
                        # floor; only a slice that got nowhere doubles it.
                        made_progress = bool(progress["n"])
                        delay = _enrich_backoff.note_failure(
                            org, after_progress=made_progress)
                        logger.warning(
                            "Provider is rate limiting org %s; %d chunk(s) enriched "
                            "before it hit. Standing enrichment down for %.0fs rather "
                            "than retrying into it (%s).",
                            org, progress["n"], delay, e)
                        worked = made_progress
                        if worked:
                            break
                    except Exception as e:
                        logger.error("Enrichment pass failed for org %s: %s", org, e)
                if not worked:
                    time.sleep(interval_sec)
            else:
                time.sleep(interval_sec)
        except Exception:
            # Anything that escaped the inner handlers: a dropped connection, a pooler
            # restart, a provider client raising something unexpected. Log it with the
            # traceback and take the next pass. The thread must outlive the error.
            logger.exception("Worker iteration failed; continuing.")
            time.sleep(interval_sec)
