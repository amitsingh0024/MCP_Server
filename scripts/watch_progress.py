#!/usr/bin/env python3
"""
Live progress for the two background stages, as plain text bars.

    python scripts/watch_progress.py                 # refresh every 20s
    python scripts/watch_progress.py -i 60           # slower
    python scripts/watch_progress.py --once          # one snapshot, no loop

Reads DATABASE_URL from .env, exactly as the CLI does, so nothing leaves this
machine. Ctrl-C to stop; it changes nothing.

Two stages run whenever the ingestion queue is empty, in this order:

  enrichment      LLM summaries and entities. Not needed for search, which ranks on
                  full text plus vectors, but `entity_lookup` is dead without it.
  vector backfill Chunks stored text-only because the embedding provider was down.
                  These are searchable lexically but not semantically, so this is
                  the one that actually degrades results.

A NOTE ON WATCHING: each refresh asks the API for /health, and an inbound request
is what keeps a Render free service from sleeping. So leaving this running is also
a keepalive. That is usually what you want while a long pass finishes, but the free
tier pools 750 instance hours a month across the workspace and a service kept awake
all month burns 720 of them, so do not leave it running for days.
"""
import os
import sys
import time
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_dotenv_if_present   # noqa: E402

load_dotenv_if_present()

from src import pgdb                            # noqa: E402

try:
    import requests
except ImportError:                              # pragma: no cover
    requests = None

HEALTH_URL = os.getenv("PDFSPECS_HEALTH_URL",
                       "https://openpdfspecs-api.onrender.com/health")
BAR_WIDTH = 42


def resolve_org(explicit: str | None) -> str:
    """The org to report on: the one given, else the one holding the chunks."""
    if explicit:
        return explicit
    with pgdb.connection() as conn:
        rows = conn.execute(
            "SELECT org_id, count(*) AS n FROM chunks GROUP BY org_id "
            "ORDER BY n DESC LIMIT 2"
        ).fetchall()
    if not rows:
        sys.exit("No chunks in the database yet.")
    if len(rows) > 1:
        print(f"Several orgs have chunks; showing the largest "
              f"({rows[0]['n']} chunks). Pass --org to pick another.\n")
    return str(rows[0]["org_id"])


def snapshot(org_id: str) -> dict:
    """Both stages' counts plus a recent rate, in one round trip each."""
    with pgdb.connection() as conn:
        counts = conn.execute(
            """
            SELECT count(*)                                         AS total,
                   count(*) FILTER (WHERE enriched_at IS NOT NULL)   AS enriched,
                   count(*) FILTER (WHERE enriched_at IS NOT NULL
                                      AND summary IS NULL)           AS no_summary,
                   count(*) FILTER (WHERE enriched_at > now() - interval '5 minutes')
                                                                     AS enriched_5m
            FROM chunks WHERE org_id = %s
            """,
            (org_id,),
        ).fetchone()
        vectors = conn.execute(
            """
            SELECT count(*) AS missing FROM chunks c
            WHERE org_id = %s
              AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id = c.id)
            """,
            (org_id,),
        ).fetchone()
        queue = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'pending')    AS pending,
                   count(*) FILTER (WHERE status = 'processing') AS processing
            FROM tasks WHERE org_id = %s
            """,
            (org_id,),
        ).fetchone()
    return {
        "total": int(counts["total"]),
        "enriched": int(counts["enriched"]),
        "no_summary": int(counts["no_summary"]),
        "enriched_5m": int(counts["enriched_5m"]),
        "vectors_missing": int(vectors["missing"]),
        "queue_pending": int(queue["pending"]),
        "queue_processing": int(queue["processing"]),
    }


def health() -> dict:
    """The worker's own view of itself. Never raises: this is a display."""
    if requests is None:
        return {"error": "requests is not installed"}
    try:
        r = requests.get(HEALTH_URL, timeout=90)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        return r.json().get("worker", {})
    except Exception as e:
        return {"error": type(e).__name__}


def bar(done: int, total: int) -> str:
    """A plain text bar. No symbols that need a legend."""
    if total <= 0:
        return "[" + " " * BAR_WIDTH + "]    n/a"
    frac = max(0.0, min(1.0, done / total))
    filled = int(round(frac * BAR_WIDTH))
    return ("[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"
            + f"  {frac * 100:5.1f}%")


def human_duration(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "unknown"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def render(snap: dict, worker: dict, baseline: dict) -> str:
    total = snap["total"]
    enriched = snap["enriched"]
    missing = snap["vectors_missing"]
    embedded = total - missing
    lines = []

    now = datetime.now(timezone.utc)
    lines.append(f"OpenPDFSpecs background progress        {now:%Y-%m-%d %H:%M:%S} UTC")
    lines.append("")

    lines.append(f"Enrichment (summaries and entities)   {enriched:,} of {total:,}")
    lines.append(f"  {bar(enriched, total)}")
    lines.append(f"  {total - enriched:,} still to do")
    lines.append("")

    lines.append(f"Vectors (semantic search)             {embedded:,} of {total:,}")
    lines.append(f"  {bar(embedded, total)}")
    lines.append(f"  {missing:,} chunks are searchable by text only")
    lines.append("")

    # Rate: measured over this session where possible, since that reflects the
    # settings now in force; the 5-minute window alone is too noisy to read an ETA
    # off, because enrichment commits in slices rather than continuously.
    rate_5m = snap["enriched_5m"] / 5.0
    elapsed_min = (time.monotonic() - baseline["at"]) / 60.0
    session_rate = None
    if elapsed_min >= 1.0:
        gained = enriched - baseline["enriched"]
        session_rate = gained / elapsed_min

    rate_line = f"Rate   last 5 min: {rate_5m:6.1f} chunks/min"
    if session_rate is not None:
        rate_line += f"      since you started watching: {session_rate:6.1f}/min"
    lines.append(rate_line)

    best = session_rate if (session_rate or 0) > 0 else rate_5m
    remaining = total - enriched
    if best > 0:
        lines.append(f"       enrichment done in about {human_duration(remaining / best * 60)}"
                     f" of running time")
    else:
        lines.append("       nothing enriched in the last 5 minutes")
    lines.append("")

    if snap["no_summary"]:
        lines.append(f"Marked done with no summary: {snap['no_summary']:,}"
                     f"   (run: python -m src.cli requeue-failed)")
    if snap["queue_pending"] or snap["queue_processing"]:
        lines.append(f"Ingestion queue: {snap['queue_pending']} pending, "
                     f"{snap['queue_processing']} processing"
                     f"   (both stages wait for this to empty)")

    lines.append("Worker " + worker_line(worker))
    lines.append("")
    lines.append("Ctrl-C to stop. This only reads.")
    return "\n".join(lines)


def worker_line(worker: dict) -> str:
    if worker.get("error"):
        return (f"unreachable ({worker['error']}). On the free plan the service sleeps "
                f"after 15 idle minutes and takes a moment to wake.")
    if not worker.get("enabled"):
        return "disabled (PDFSPECS_DISABLE_WORKER is set), so neither stage will run."
    if not worker.get("alive"):
        return "thread is DEAD while the web server still answers. It needs a restart."

    # Both are time.time() values, so compare them against now, not each other.
    # `heartbeat` is stamped at the TOP of each loop pass, so during a long pass it
    # stays put — heartbeat minus started_at reads as zero uptime on a worker that has
    # been up for hours and is busy. Its age is the useful number: a few minutes is
    # normal mid-slice, half an hour means the pass is wedged.
    parts = []
    now = time.time()
    started = worker.get("started_at")
    heartbeat = worker.get("heartbeat")
    if started:
        parts.append(f"up {human_duration(now - started)}")
    if heartbeat:
        parts.append(f"last pass began {human_duration(now - heartbeat)} ago")
    parts.append(f"{worker.get('iterations', '?')} loop passes")
    if worker.get("task_id"):
        parts.append(f"ingesting task {worker['task_id']}")
    else:
        parts.append("idle, so on the backfill stages")

    out = "running: " + ", ".join(parts)
    for stage, label, floor_env, floor_default in (
            ("embed_backoff", "vector backfill", "PDFSPECS_EMBED_BACKOFF_SEC", 300.0),
            ("enrich_backoff", "enrichment", "PDFSPECS_ENRICH_BACKOFF_SEC", 30.0)):
        info = worker.get(stage)
        if not info:
            continue
        remaining = info.get("resumes_in_sec", 0)
        waiting = human_duration(remaining)
        # Two different situations, and calling both "intended behaviour" is how a
        # ratchet to the ceiling went unnoticed for three hours. A stage that did work
        # and then met a rate limit is pacing itself; a stage whose delay is doubling
        # cannot get anywhere. `escalated_to_sec` says which, but only a server running
        # the fix sends it, so fall back to the floor: a wait well above it can only be
        # an escalated delay whoever is reporting it.
        escalated = info.get("escalated_to_sec")
        floor = float(os.getenv(floor_env, floor_default))
        if escalated:
            out += (f"\n       {label} got nowhere and is now waiting "
                    f"{human_duration(escalated)} between tries, {waiting} left. "
                    f"Climbing numbers here mean the provider is still down.")
        elif remaining > floor * 1.5:
            out += (f"\n       {label} is standing down for {waiting}, well past its "
                    f"{human_duration(floor)} floor, so the delay has escalated. This "
                    f"server build does not report it directly.")
        else:
            out += (f"\n       {label} is pacing itself: {waiting} to go, after a slice "
                    f"that did work and then hit the provider's rate limit. Normal.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    ap.add_argument("-i", "--interval", type=float, default=20.0,
                    help="seconds between refreshes (default 20)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--org", default=None, help="org id (default: the one with chunks)")
    args = ap.parse_args()

    org = resolve_org(args.org)
    first = snapshot(org)
    baseline = {"at": time.monotonic(), "enriched": first["enriched"]}
    live = sys.stdout.isatty() and not args.once

    while True:
        snap = first if first else snapshot(org)
        first = None
        text = render(snap, health(), baseline)
        if live:
            sys.stdout.write("\033[2J\033[H" + text + "\n")
            sys.stdout.flush()
        else:
            print(text)
        if not live:
            return 0
        try:
            time.sleep(max(2.0, args.interval))
        except KeyboardInterrupt:
            print()
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
