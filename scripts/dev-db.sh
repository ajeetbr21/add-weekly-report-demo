#!/usr/bin/env bash
# Standalone PostgreSQL + pgvector for running the backend outside compose (local development).
#   scripts/dev-db.sh start|stop
# Uses POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB from the environment (default atlas/atlas/atlas),
# e.g.  set -a; . ./.env; set +a; scripts/dev-db.sh start
# DEVDB_HOST_NETWORK=1 uses host networking (for environments where -p port publishing does not work).
set -euo pipefail
NAME=${DEVDB_NAME:-atlas-devdb}
USER_=${POSTGRES_USER:-atlas}
PASS_=${POSTGRES_PASSWORD:-atlas}
DB_=${POSTGRES_DB:-atlas}
case "${1:-start}" in
  start)
    if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
      docker start "$NAME" >/dev/null
    else
      if [ "${DEVDB_HOST_NETWORK:-0}" = "1" ]; then NET=(--network host); else NET=(-p 127.0.0.1:5432:5432); fi
      docker run -d --name "$NAME" "${NET[@]}" -e POSTGRES_USER="$USER_" -e POSTGRES_PASSWORD="$PASS_" \
        -e POSTGRES_DB="$DB_" -v atlas-devdb-data:/var/lib/postgresql/data docker.io/pgvector/pgvector:pg16 >/dev/null
    fi
    for _ in $(seq 1 30); do
      if docker exec "$NAME" pg_isready -U "$USER_" >/dev/null 2>&1; then
        echo "postgres ready: postgresql+asyncpg://$USER_:***@localhost:5432/$DB_"; exit 0
      fi
      sleep 1
    done
    echo "postgres did not become ready" >&2; exit 1 ;;
  stop) docker rm -f "$NAME" >/dev/null && echo "stopped (data kept in volume atlas-devdb-data)" ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
