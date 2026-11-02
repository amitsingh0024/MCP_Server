"""
Local command-line interface (REVAMP Phase B).

The local counterpart to the admin dashboard. The dashboard authenticates through
Supabase, which a local-only install doesn't have — so without this there was no way to
ingest a PDF or inspect the knowledgebase locally at all.

Everything here runs against the org resolved by src.auth (the implicit local org under
PDFSPECS_LOCAL_MODE=1).

    python -m src.cli init                    # create/upgrade the schema
    python -m src.cli set-key nvapi-...       # store the NVIDIA key (encrypted)
    python -m src.cli ingest FILE.pdf [...]   # queue PDFs for ingestion
    python -m src.cli worker                  # process the queue (blocks)
    python -m src.cli ls                      # list ingested documents
    python -m src.cli status                  # show queue state
    python -m src.cli search "query"          # hybrid search
    python -m src.cli info                    # show resolved configuration
"""
import os
import sys
import json
import argparse
import logging
from pathlib import Path

from src.config import load_dotenv_if_present

load_dotenv_if_present()

import src.auth as auth          # noqa: E402  - must follow load_dotenv_if_present()
import src.pgdb as pgdb          # noqa: E402
import src.storage as storage    # noqa: E402
import src.search as search      # noqa: E402
import src.ingest as ingest      # noqa: E402
import src.migrate as migrate    # noqa: E402
import src.enricher as enricher  # noqa: E402

logger = logging.getLogger("pdfspecs.cli")


def _org() -> str:
    """Resolve the org for CLI operations."""
    if auth.local_mode():
        return auth.local_org_id()
    key = os.getenv("PDFSPECS_API_KEY", "").strip()
    org_id = auth.resolve_api_key(key) if key else None
    if not org_id:
        raise SystemExit(
            "No org resolved. Set PDFSPECS_LOCAL_MODE=1 in .env for a local install, "
            "or PDFSPECS_API_KEY=sk-pdfspecs-... to target a specific org."
        )
    return org_id


# =====================================================================
# Commands
# =====================================================================
def cmd_init(args) -> int:
    n = migrate.run()
    print(f"Applied {n} migration(s)." if n else "Database is already up to date.")
    print(f"Org: {_org()}")
    return 0


def cmd_set_key(args) -> int:
    pgdb.save_setting(_org(), "nvidia_api_key", args.key, encrypt=True)
    print("NVIDIA API key saved (encrypted).")
    return 0


def cmd_ingest(args) -> int:
    org = _org()
    queued = 0
    for raw in args.paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"  skip (not a file): {path}", file=sys.stderr)
            continue
        data = path.read_bytes()
        key = storage.upload_bytes(org, path.name, data)
        task_id = pgdb.add_task(org, key, path.name)
        print(f"  queued task {task_id}: {path.name} ({len(data)/1024:.0f} KB)")
        queued += 1
    if not queued:
        print("Nothing queued.", file=sys.stderr)
        return 1
    print(f"\nQueued {queued} file(s). Run `python -m src.cli worker` to process them.")
    return 0


def cmd_worker(args) -> int:
    _org()  # fail fast if the org can't be resolved
    print("Worker running (Ctrl-C to stop)...")
    try:
        ingest.run_worker_loop(max_runs=args.max_runs, enrich=not args.no_enrich)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_enrich(args) -> int:
    """Stage 2: backfill LLM summaries + entities for chunks stored by ingestion."""
    org = _org()
    pending, total = pgdb.count_pending_enrichment(org)
    if not pending:
        print(f"Nothing to enrich ({total} chunks, all done).")
        return 0

    target = pending if args.limit is None else min(pending, args.limit)
    print(f"Enriching {target} of {pending} pending chunks ({total} total)...")

    def progress(done):
        pct = 100 * done / max(1, target)
        print(f"\r  {done}/{target}  ({pct:.0f}%)", end="", flush=True)

    try:
        done = ingest.run_enrichment_pass(org, limit=args.limit, progress_cb=progress)
    except KeyboardInterrupt:
        print("\nStopped — progress is saved; rerun to resume.")
        return 0
    except enricher.APIKeyMissingError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1

    remaining, _ = pgdb.count_pending_enrichment(org)
    print(f"\nEnriched {done} chunk(s). {remaining} still pending.")
    return 0


def cmd_ls(args) -> int:
    docs = pgdb.list_documents(_org())
    if not docs:
        print("No documents ingested yet.")
        return 0
    print(f"{'FILENAME':<50} {'SIZE':>10}  INGESTED")
    for d in docs:
        print(f"{d['filename'][:50]:<50} {d['size_bytes']/1024:>9.0f}K  "
              f"{d['created_at']:%Y-%m-%d %H:%M}")
    print(f"\n{len(docs)} document(s).")
    return 0


def cmd_status(args) -> int:
    tasks = pgdb.list_tasks(_org())
    if not tasks:
        print("Queue is empty.")
        return 0
    for t in tasks:
        progress = ""
        if t.get("progress_info"):
            try:
                p = json.loads(t["progress_info"])
                progress = f"  {p.get('phase','')}: {p.get('detail','')} ({p.get('progress',0)}%)"
            except (ValueError, TypeError):
                progress = f"  {t['progress_info']}"
        print(f"[{t['status']:<10}] #{t['id']} {t.get('original_filename') or ''}{progress}")
        if t.get("error_message"):
            print(f"             error: {t['error_message']}")
    return 0


def cmd_search(args) -> int:
    rows = search.hybrid_search(_org(), args.query, args.limit)
    if not rows:
        print(f"No matches for: {args.query!r}")
        return 0
    for i, r in enumerate(rows, 1):
        print(f"\n{i}. {r['filename']} (pages {r['page_start']}-{r['page_end']})  "
              f"score={r['score']:.4f}")
        print(f"   id: {r['id']}")
        if r.get("summary"):
            print(f"   {r['summary']}")
        snippet = (r["text_content"] or "")[:300].replace("\n", " ")
        print(f"   \"{snippet}...\"")
    return 0


def cmd_info(args) -> int:
    org = _org()
    has_key = bool(pgdb.get_setting(org, "nvidia_api_key", decrypt=True))
    print(f"  org_id           {org}")
    print(f"  local mode       {auth.local_mode()}")
    print(f"  database         {os.getenv('DATABASE_URL', '(unset)')}")
    print(f"  storage backend  {storage.backend()}")
    if storage.backend() == "local":
        print(f"  data dir         {storage.data_dir()}")
    print(f"  nvidia key       {'configured' if has_key else 'NOT SET — run set-key'}")
    print(f"  llm model        {pgdb.get_setting(org, 'llm_model') or enricher.NVIDIA_LLM_MODEL}")
    print(f"  embedding        {enricher.default_embedding_model()} "
          f"({pgdb.EMBEDDING_DIM}-d, backend={enricher.embedding_backend()})")
    print(f"  documents        {len(pgdb.list_documents(org))}")
    pending, total = pgdb.count_pending_enrichment(org)
    enriched = total - pending
    print(f"  chunks           {total} stored (searchable)")
    print(f"  enrichment       {enriched}/{total} done"
          + (f" — {pending} pending, run `enrich`" if pending else ""))
    print("\n  migrations:")
    for name, state in migrate.status():
        print(f"    {state:<26} {name}")
    return 0


# =====================================================================
# Entrypoint
# =====================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.cli",
                                description="OpenPDFSpecs local CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create/upgrade the database schema").set_defaults(func=cmd_init)

    sp = sub.add_parser("set-key", help="store the NVIDIA API key (encrypted)")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_set_key)

    sp = sub.add_parser("ingest", help="queue one or more PDFs for ingestion")
    sp.add_argument("paths", nargs="+")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("worker", help="process the ingestion queue")
    sp.add_argument("--max-runs", type=int, default=None,
                    help="stop after processing this many tasks")
    sp.add_argument("--no-enrich", action="store_true",
                    help="don't backfill enrichment while the queue is idle")
    sp.set_defaults(func=cmd_worker)

    sp = sub.add_parser("enrich", help="backfill LLM summaries + entities (stage 2)")
    sp.add_argument("-n", "--limit", type=int, default=None,
                    help="enrich at most this many chunks, then stop")
    sp.set_defaults(func=cmd_enrich)

    sub.add_parser("ls", help="list ingested documents").set_defaults(func=cmd_ls)
    sub.add_parser("status", help="show the ingestion queue").set_defaults(func=cmd_status)
    sub.add_parser("info", help="show resolved configuration").set_defaults(func=cmd_info)

    sp = sub.add_parser("search", help="hybrid search the knowledgebase")
    sp.add_argument("query")
    sp.add_argument("-n", "--limit", type=int, default=5)
    sp.set_defaults(func=cmd_search)

    return p


def main(argv=None) -> int:
    logging.basicConfig(level=os.getenv("PDFSPECS_LOG_LEVEL", "WARNING").upper(),
                        format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except auth.AuthError as e:
        # Configuration problems (e.g. local mode aimed at a remote database) are the
        # user's to fix — show the guidance, not a traceback.
        print(f"\n{e}\n", file=sys.stderr)
        return 2
    finally:
        pgdb.close_pool()


if __name__ == "__main__":
    sys.exit(main())
