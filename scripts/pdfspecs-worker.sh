#!/bin/bash
# OpenPDFSpecs ingestion worker — launchd wrapper.
#
# launchd starts agents as soon as the user logs in, which is usually BEFORE Postgres is
# accepting connections. Without this wait the worker would exit immediately, launchd would
# restart it, and the pair would crash-loop through login. Waiting here keeps the retry
# logic in one obvious place.
#
# Installed as ~/Library/LaunchAgents/com.pdfspecs.worker.plist — see scripts/install-worker-agent.sh

set -uo pipefail

PROJECT_DIR="/Volumes/Untitled/proj/work_stuff"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PSQL_READY="/usr/local/opt/postgresql@14/bin/pg_isready"
LOG_DIR="$HOME/.pdfspecs/logs"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

# Wait for Postgres — up to 5 minutes. The project volume may also still be mounting.
for _ in $(seq 1 60); do
    if [ -x "$PSQL_READY" ] && "$PSQL_READY" -q 2>/dev/null; then
        break
    fi
    sleep 5
done

if [ -x "$PSQL_READY" ] && ! "$PSQL_READY" -q 2>/dev/null; then
    echo "$(date '+%F %T') postgres not reachable after 5 min; exiting for launchd to retry" >&2
    exit 1
fi

# The project volume is external; without it there is nothing to run.
if [ ! -x "$PYTHON" ]; then
    echo "$(date '+%F %T') venv not found at $PYTHON (is the volume mounted?); exiting" >&2
    exit 1
fi

echo "$(date '+%F %T') starting ingestion worker" >&2

# Enrichment ON: once the ingest queue drains, the worker spends idle time backfilling
# summaries and entities instead of sitting idle.
exec "$PYTHON" -m src.cli worker
