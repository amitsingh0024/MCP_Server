"""
Migration runner (REVAMP Phase B).

Applies `db/migrations/*.sql` in filename order, recording each in a `schema_migrations`
table so re-running is a no-op. Previously migrations were pasted by hand into the
Supabase SQL editor, which meant no record of what had been applied to which database —
unworkable once there's a local database as well as the hosted one.

Files marked `@supabase-only` (currently the RLS policies, which reference Supabase's
`auth.uid()`) are skipped automatically on a database with no `auth` schema.

Usage:
    python -m src.migrate            # apply pending migrations
    python -m src.migrate --status   # list applied/pending, apply nothing
"""
import sys
import logging
from pathlib import Path

from src.config import load_dotenv_if_present

# Load .env before pgdb reads DATABASE_URL. `python -m src.migrate` is a documented
# entrypoint but got no shell profile and no .env, so it failed with "DATABASE_URL is not
# set" on a machine where every other command worked; only `src.cli init` loaded it.
load_dotenv_if_present()

import src.pgdb as pgdb              # noqa: E402  - must follow load_dotenv_if_present()

logger = logging.getLogger("pdfspecs.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
SUPABASE_ONLY_MARKER = "@supabase-only"


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def _applied(conn) -> set:
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r["filename"] for r in rows}


def _has_auth_schema(conn) -> bool:
    """True on Supabase (or any DB with an `auth` schema); False on a plain local Postgres."""
    row = conn.execute(
        "SELECT 1 AS ok FROM information_schema.schemata WHERE schema_name = 'auth'"
    ).fetchone()
    return bool(row)


def migration_files() -> list:
    if not MIGRATIONS_DIR.is_dir():
        raise RuntimeError(f"No migrations directory at {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def status() -> list:
    """Return [(filename, state)] where state is 'applied' | 'pending' | 'skipped'."""
    with pgdb.connection() as conn:
        _ensure_table(conn)
        done = _applied(conn)
        supabase = _has_auth_schema(conn)
        out = []
        for path in migration_files():
            if path.name in done:
                out.append((path.name, "applied"))
            elif SUPABASE_ONLY_MARKER in path.read_text() and not supabase:
                out.append((path.name, "skipped (supabase-only)"))
            else:
                out.append((path.name, "pending"))
        return out


def run() -> int:
    """Apply every pending migration. Returns the number applied."""
    applied_count = 0
    with pgdb.connection() as conn:
        _ensure_table(conn)
        done = _applied(conn)
        supabase = _has_auth_schema(conn)

        for path in migration_files():
            if path.name in done:
                continue
            sql = path.read_text()
            if SUPABASE_ONLY_MARKER in sql and not supabase:
                logger.info("Skipping %s (supabase-only; no auth schema here)", path.name)
                continue

            logger.info("Applying %s ...", path.name)
            # Each file runs in its own transaction: a failure leaves the database on the
            # last good migration rather than half-way through a broken one.
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
            applied_count += 1
            logger.info("Applied %s", path.name)

    return applied_count


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    argv = argv if argv is not None else sys.argv[1:]

    # Close the pool explicitly: its worker threads can't be joined during interpreter
    # shutdown, which surfaces as a PythonFinalizationError traceback after a clean run.
    try:
        if "--status" in argv:
            for name, state in status():
                print(f"  {state:<26} {name}")
            return 0

        n = run()
        print(f"Applied {n} migration(s)." if n else "Database is up to date.")
        return 0
    finally:
        pgdb.close_pool()


if __name__ == "__main__":
    sys.exit(main())
