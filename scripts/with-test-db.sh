#!/usr/bin/env bash
# Runs a command against an ephemeral PostgreSQL+pgvector container, then removes it.
#   scripts/with-test-db.sh <command...>
# Example (from repo root):
#   scripts/with-test-db.sh bash -c "cd backend && .venv/bin/pytest -q"
# Env:
#   TESTDB_PORT          host port (default 55432)
#   TESTDB_HOST_NETWORK  1 = use host networking (for environments where -p publishing does not work)
set -euo pipefail
PORT="${TESTDB_PORT:-55432}"
NAME="atlas-testdb-$$"
IMAGE="docker.io/pgvector/pgvector:pg16"

if [ "${TESTDB_HOST_NETWORK:-0}" = "1" ]; then
  NET=(--network host -e PGPORT="$PORT")
else
  NET=(-p "127.0.0.1:${PORT}:5432")
fi

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$NAME" "${NET[@]}" \
  -e POSTGRES_USER=atlas -e POSTGRES_PASSWORD=atlas -e POSTGRES_DB=atlas_test \
  "$IMAGE" -c fsync=off -c synchronous_commit=off >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U atlas -d atlas_test -p "$([ "${TESTDB_HOST_NETWORK:-0}" = "1" ] && echo "$PORT" || echo 5432)" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
sleep 1

export DATABASE_URL="postgresql+asyncpg://atlas:atlas@127.0.0.1:${PORT}/atlas_test"
echo "[with-test-db] DATABASE_URL=${DATABASE_URL}"
"$@"
