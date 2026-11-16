#!/usr/bin/env python
"""
Ingest ONE book end to end with a live progress bar, then enrich it.

Usage:  python scripts/run_book.py <pdf> [--org ORG_ID] [--no-enrich]

Exists because `cli ingest` resolves the org through an API key, and the only keys
on this deployment are stored hashed. This drives the same ingest/enrich functions
directly against a known org id.

Stage 1 (parse -> OCR -> chunk -> embed -> store) and stage 2 (LLM enrichment) are
both resumable, so Ctrl-C loses at most the page in flight.
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_dotenv_if_present

load_dotenv_if_present()

from src import ingest, pgdb, storage, enricher  # noqa: E402
from src.config import Config  # noqa: E402

DEFAULT_ORG = "fac2e9b4-5bc2-433c-bfc4-ea696af12dbe"
BAR_W = 34


def _fmt(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


class Bar:
    """One-line progress bar that also records phase timings."""

    def __init__(self, label):
        self.label = label
        self.t0 = time.time()
        self.last = ""
        self.page_t0 = None
        self.page_n = 0

    def render(self, pct, phase, detail, eta=None):
        pct = max(0, min(100, int(pct)))
        fill = int(BAR_W * pct / 100)
        bar = "#" * fill + "." * (BAR_W - fill)
        el = _fmt(time.time() - self.t0)
        tail = f" eta {_fmt(eta)}" if eta else ""
        line = f"\r[{bar}] {pct:3d}%  {phase:<13} {detail:<34} {el}{tail}"
        # pad so a shorter line fully overwrites a longer previous one
        pad = max(0, len(self.last) - len(line))
        sys.stdout.write(line + " " * pad)
        sys.stdout.flush()
        self.last = line

    def done(self, msg):
        sys.stdout.write("\r" + " " * (len(self.last) + 4) + "\r")
        print(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--org", default=os.getenv("PDFSPECS_ORG_ID", DEFAULT_ORG))
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--log", default=None, help="also write the full log here")
    args = ap.parse_args()

    path = Path(args.pdf).expanduser().resolve()
    if not path.exists():
        sys.exit(f"not found: {path}")

    # With --log the bar owns the terminal and the log goes to the file only;
    # interleaving INFO lines with a \r-updated bar makes both unreadable.
    handlers = [logging.FileHandler(args.log)] if args.log \
        else [logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # Keep the bar readable: INFO lines from the pipeline go to the log file/stderr,
    # the bar owns stdout.
    logging.getLogger("pdfspecs").setLevel(logging.INFO)

    cfg = Config()
    print(f"book        : {path.name}")
    print(f"org         : {args.org}")
    print(f"ocr         : {cfg.ocr_provider} (mode={cfg.ocr_mode}) "
          f"-> {os.getenv('PDFSPECS_OCR_SERVICE_URL', 'n/a')}")
    try:
        _os = ingest._org_provider_settings(args.org)
        _model = _os.resolved_embedding_model()
    except Exception as e:
        _model = f"(unresolved: {type(e).__name__})"
    print(f"embeddings  : {enricher.embedding_backend()} / {_model}")
    print(f"embed batch : {enricher.NVIDIA_EMBED_BATCH} window(s) per request")
    print()

    # ---- queue, already claimed by us ------------------------------------
    # Inserted straight into 'processing' with our own worker id, rather than
    # add_task() + get_next_task(). Those are two statements, and a hosted worker
    # polling the queue can claim the row in the gap between them -- then it ingests
    # the book with whatever OCR configuration that environment happens to have,
    # which on the deployed service means no OCR endpoint and a silent fall back to
    # Tesseract. Inserting it pre-claimed means no other worker ever sees it pending.
    #
    # Nothing else needs to change: the 15-minute lease plus this runner's per-page
    # progress writes already stop another worker reclaiming it mid-run.
    data = path.read_bytes()
    key = storage.upload_bytes(args.org, path.name, data)
    me = f"runbook:{os.getpid()}"
    with pgdb.connection() as conn:
        row = conn.execute(
            """INSERT INTO tasks (org_id, storage_key, original_filename,
                                  status, attempts, locked_by)
               VALUES (%s, %s, %s, 'processing', 1, %s) RETURNING id""",
            (args.org, key, path.name, me),
        ).fetchone()
        task_id = int(row["id"])
        conn.commit()
    task = {"id": task_id, "org_id": args.org, "storage_key": key,
            "original_filename": path.name, "attempts": 0}
    print(f"queued task {task_id} (pre-claimed by {me}, {len(data)//1024} KB)\n")

    # ---- stage 1 with a live bar ----------------------------------------
    bar = Bar("ingest")
    original_progress = ingest._progress

    def patched(tid, phase, detail, progress):
        original_progress(tid, phase, detail, progress)
        eta = None
        # During parsing, estimate from the observed page rate; parsing dominates
        # wall time on this corpus (OCR is ~80s/page) while occupying only the
        # first 10% of the progress scale.
        if phase == "Parsing PDF" and "page" in detail:
            try:
                cur, total = detail.split("page ")[1].split(" of ")
                cur, total = int(cur), int(total)
                if bar.page_t0 is None:
                    bar.page_t0, bar.page_n = time.time(), cur
                elif cur > bar.page_n:
                    rate = (time.time() - bar.page_t0) / max(1, cur - bar.page_n)
                    eta = rate * (total - cur)
            except Exception:
                pass
        bar.render(progress, phase, detail, eta)

    ingest._progress = patched
    t0 = time.time()
    try:
        # No get_next_task(): the row above was inserted already claimed, so there is
        # no pending window for another worker to win.
        ok = ingest.process_task(task)
    finally:
        ingest._progress = original_progress
    stage1 = time.time() - t0
    bar.done(f"stage 1 {'complete' if ok else 'FAILED'} in {_fmt(stage1)}")
    if not ok:
        row = pgdb.get_task(task_id) if hasattr(pgdb, "get_task") else None
        print("  error:", (row or {}).get("error_message", "(see log)"))
        return 1

    pending_emb, total_chunks = pgdb.count_pending_embedding(args.org)
    print(f"  chunks stored   : {total_chunks}")
    print(f"  awaiting vectors: {pending_emb}")

    if pending_emb:
        print("\nembedding backfill:")
        b2 = Bar("embed")
        ingest.run_embedding_pass(
            args.org,
            progress_cb=lambda d: b2.render(100 * d / max(1, pending_emb),
                                            "Embedding", f"{d}/{pending_emb} chunks"))
        b2.done(f"  embedding backfill done in {_fmt(time.time() - b2.t0)}")

    # ---- stage 2: enrichment --------------------------------------------
    if args.no_enrich:
        print("\nskipping enrichment (--no-enrich)")
        return 0

    pend, tot = pgdb.count_pending_enrichment(args.org)
    print(f"\nenrichment: {pend} of {tot} chunks pending")
    if pend:
        b3 = Bar("enrich")
        t1 = time.time()
        done = ingest.run_enrichment_pass(
            args.org,
            progress_cb=lambda d: b3.render(
                100 * d / max(1, pend), "Enriching", f"{d}/{pend} chunks",
                ((time.time() - t1) / max(1, d)) * (pend - d) if d else None))
        b3.done(f"  enriched {done} chunk(s) in {_fmt(time.time() - t1)}")

    pend2, tot2 = pgdb.count_pending_enrichment(args.org)
    emb2, _ = pgdb.count_pending_embedding(args.org)
    print(f"\nfinal: {tot2} chunks | {tot2 - pend2} enriched | "
          f"{tot2 - emb2} with vectors | total {_fmt(time.time() - t0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
