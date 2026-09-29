#!/usr/bin/env bash
# One-command setup for Atlas. Safe to re-run: it never overwrites secrets you already have.
#
#   scripts/bootstrap.sh            set up, build and start the stack
#   scripts/bootstrap.sh --demo     …and run the end-to-end demo afterwards
#   scripts/bootstrap.sh --no-build use existing images (faster restart)
#
# What it does: checks prerequisites, creates .env with freshly generated secrets, builds the images,
# starts all services, applies migrations, waits for readiness, then prints the URLs, your API token and
# the status of every integration.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
RUN_DEMO=0
BUILD_FLAG="--build"
for a in "$@"; do
  case "$a" in
    --demo) RUN_DEMO=1 ;;
    --no-build) BUILD_FLAG="" ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. prerequisites
bold "1/6  Checking prerequisites"
command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux), then re-run."
docker info >/dev/null 2>&1 || die "the Docker daemon is not responding. Start Docker Desktop (or 'sudo systemctl start docker') and re-run."

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
elif docker help 2>/dev/null | grep -q " compose "; then
  COMPOSE=(docker compose)
else
  die "Docker Compose v2 not found. Docker Desktop includes it; on Linux install the docker-compose-plugin package."
fi
ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')  |  compose: ${COMPOSE[*]}"

# advisory only: the omniroute and letta images are large
TOTAL_MB=$(docker info --format '{{.MemTotal}}' 2>/dev/null | awk '{printf "%d", $1/1048576}')
[ -n "${TOTAL_MB:-}" ] && [ "${TOTAL_MB:-0}" -gt 0 ] && {
  [ "$TOTAL_MB" -lt 6000 ] && warn "Docker has ${TOTAL_MB} MB RAM. 8 GB+ is recommended (raise it in Docker Desktop → Settings → Resources)." \
                           || ok "Docker memory: ${TOTAL_MB} MB"
}

# ---------------------------------------------------------------- 2. secrets / .env
bold "2/6  Preparing .env"
gen_secret() {
  if command -v python3 >/dev/null 2>&1; then python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
  elif command -v openssl  >/dev/null 2>&1; then openssl rand -base64 32 | tr -d '/+=\n'
  else head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
set_var() {  # set_var KEY VALUE  — only replaces the value if it is still a placeholder or empty
  local key="$1" val="$2" cur
  cur=$(grep -E "^${key}=" .env | head -1 | cut -d= -f2-)
  case "$cur" in
    ""|change-me|change-me-*|atlas-local-password)
      # portable in-place edit (GNU and BSD sed differ on -i)
      sed "s|^${key}=.*|${key}=${val}|" .env > .env.tmp && mv .env.tmp .env
      return 0 ;;
    *) return 1 ;;
  esac
}

if [ -f .env ]; then
  ok ".env already exists — keeping your values"
else
  cp .env.example .env
  ok "created .env from .env.example"
fi
for key in ATLAS_API_TOKEN APPLICATION_SECRET POSTGRES_PASSWORD LOCAL_MCP_TOKEN; do
  if set_var "$key" "$(gen_secret)"; then ok "generated $key"; else ok "$key already set — left unchanged"; fi
done
API_TOKEN=$(grep -E '^ATLAS_API_TOKEN=' .env | cut -d= -f2-)

# ---------------------------------------------------------------- 3. build + start
bold "3/6  Building images and starting the stack (first run pulls ~6 GB, be patient)"
"${COMPOSE[@]}" up -d $BUILD_FLAG || die "'compose up' failed. Scroll up for the reason, or run: ${COMPOSE[*]} logs"
ok "containers started"

# ---------------------------------------------------------------- 4. wait for readiness
bold "4/6  Waiting for the API to become ready (migrations run automatically)"
READY=""
for i in $(seq 1 100); do
  READY=$("${COMPOSE[@]}" exec -T api curl -fs http://localhost:8000/ready 2>/dev/null) && break
  sleep 3
  [ $((i % 10)) -eq 0 ] && printf '  … still waiting (%ss)\n' "$((i * 3))"
done
if [ -z "$READY" ]; then
  warn "the API did not report ready in ~5 minutes. Diagnostics:"
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail=30 migrate api
  die "see docs/troubleshooting.md"
fi
ok "ready: $READY"

# Letta starts slower than the API; wait for it so the status below is accurate rather than alarming.
if grep -qiE '^LETTA_ENABLED=[[:space:]]*true' .env; then
  for i in $(seq 1 40); do
    "${COMPOSE[@]}" exec -T api python -c \
      "import httpx,sys; sys.exit(0 if httpx.get('http://letta:8283/v1/health/',timeout=3).status_code==200 else 1)" \
      >/dev/null 2>&1 && { ok "letta session runtime ready"; break; }
    [ "$i" = 1 ] && printf '  … waiting for the Letta session runtime\n'
    sleep 3
  done
fi

# ---------------------------------------------------------------- 5. integrations
bold "5/6  Integration status"
"${COMPOSE[@]}" exec -T api python - <<'PY' 2>/dev/null || warn "could not read /api/integrations yet"
import os, httpx
h = {"Authorization": "Bearer " + os.environ.get("ATLAS_API_TOKEN", "")}
data = httpx.get("http://localhost:8000/api/integrations", headers=h, timeout=90).json()
MARKS = {"OK": "\033[32m✓\033[0m", "CONFIGURATION_REQUIRED": "\033[33m!\033[0m",
         "DISABLED": "\033[33m-\033[0m", "UNREACHABLE": "\033[31m✗\033[0m"}
for name, info in data.items():
    status = info.get("status")
    if not status:          # 'webhooks' and 'auth' are settings, not integrations
        continue
    detail = str(info.get("detail") or info.get("version") or info.get("tools") or "")[:70]
    print(f"  {MARKS.get(status, '?')} {name:<10} {status:<24} {detail}")
auth = data.get("auth", {})
print(f"  \033[32m✓\033[0m auth       API token {'required' if auth.get('api_token_required') else 'NOT set (anyone on this machine can call the API)'}")
PY
cat <<'TXT'
  CONFIGURATION_REQUIRED is expected: those integrations need your own credentials.
  Atlas works without them — agents then run in a clearly labelled offline mode.
TXT

# ---------------------------------------------------------------- 6. done
bold "6/6  Ready"
cat <<TXT

  Web UI      http://localhost:3000
  API docs    http://localhost:8000/docs
  OmniRoute   http://localhost:20128      (connect a model provider here)

  Your API token (already injected into the Web UI, needed for direct API calls):
    ${API_TOKEN}

  Try it:     open the Web UI and enter
              "Analyze today's AWS alarms and summarize anything that needs attention."
  Demo:       scripts/demo.sh
  Logs:       ${COMPOSE[*]} logs -f api worker
  Stop:       ${COMPOSE[*]} down          (data is kept; add -v to delete it)

  Turn on real LLM reasoning:  docs/setup.md  →  "Connect a model provider"
  Everything else:             README.md, AI.md, docs/

TXT

if [ "$RUN_DEMO" = "1" ]; then
  bold "Running the end-to-end demo"
  "$ROOT/scripts/demo.sh" || warn "the demo reported failures — see the output above and docs/troubleshooting.md"
fi
