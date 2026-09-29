# Troubleshooting

Start here:

```bash
docker compose ps                                   # which services are up
curl -s localhost:8000/ready                        # database, migration, pgvector
curl -s localhost:8000/api/integrations -H "Authorization: Bearer $ATLAS_API_TOKEN" | python3 -m json.tool
docker compose logs --tail=50 api worker
```

The Integrations page in the UI shows the same status, and every task's journal explains what happened step
by step.

## Startup

**`migrate` shows `Exited (0)`** — that is correct. It applies the migrations and exits.

**`/ready` returns 503 `NOT_MIGRATED`** — migrations did not run. `docker compose logs migrate`, then
`docker compose up -d migrate`.

**`/ready` returns `UNREACHABLE`** — PostgreSQL is not up yet (`docker compose logs postgres`). On the very
first start the database volume is initialized, which takes a few seconds.

**Authentication failures against PostgreSQL** — `POSTGRES_PASSWORD` was changed after the volume was
created. The password is baked into the volume on first init. Either restore the old value, or reset:
`docker compose down -v` (deletes all data).

**Port already in use** — something else owns 8000, 3000 or 20128. Change the host side of the mapping in
`docker-compose.yml`, e.g. `ports: ["127.0.0.1:8001:8000"]`.

**`omniroute` or `letta` will not start / the machine runs out of memory** — these are the heavy images.
Give Docker more RAM, lower `OMNIROUTE_MEMORY_MB`, or stop what you do not need:
`docker compose stop letta` (set `LETTA_ENABLED=false`) and `docker compose stop omniroute`. The rest of
Atlas keeps working.

## Everything says "offline mode"

Expected until a model provider is connected. The result reads
`Offline mode (no LLM available: …)` and the `MODEL_CALL` journal event holds the full reason.

| Reason in the event | Fix |
|---|---|
| `OmniRoute not enabled` | set `OMNIROUTE_ENABLED=true` in `.env` and restart `api` and `worker` |
| `Gateway rejected API key (HTTP 401)` | create a key in the OmniRoute dashboard and set `OMNIROUTE_API_KEY` |
| `HTTP 403 … free tier can only be used from within …` | the bundled free providers refused. Connect a provider you have credentials for at http://localhost:20128 |
| `HTTP 400 … Model is unavailable` | the model in `MODEL_*` does not exist for your providers. Pick one the dashboard lists |
| `OmniRoute recently failed, retrying later` | circuit breaker after a failure; it retries after `OMNIROUTE_FAILURE_COOLDOWN_SECONDS` (60 s) |
| `ConnectError` / `UNREACHABLE` | the container is down (`docker compose up -d omniroute`), or `OMNIROUTE_BASE_URL` is wrong — inside Compose it must be `http://omniroute:20128/v1` |

Verify the gateway directly:

```bash
docker compose exec -T api python -c "
import httpx; r = httpx.post('http://omniroute:20128/v1/chat/completions',
  json={'model':'auto','messages':[{'role':'user','content':'Say OK'}]}, timeout=60)
print(r.status_code, r.text[:400])"
```

Set `LLM_OFFLINE_FALLBACK=false` to make tasks fail loudly instead of degrading — useful while debugging
a provider.

## Tasks

**A task stays `PENDING`** — the worker is not running or cannot reach the database.
`docker compose ps worker`, `docker compose logs --tail=50 worker`, `docker compose up -d worker`.
Queued tasks are picked up as soon as it starts; nothing is lost.

**Stuck in `WAITING_APPROVAL`** — by design. Approve or reject it: Approvals page,
`GET /api/approvals?status=PENDING`, or `/approvals` in Telegram.

**`RETRYING` repeatedly, then `FAILED`** — the step raised an exception each time. The `STEP_FAILED` journal
events carry the error. Budget is `TASK_MAX_RETRIES` (2) plus one re-attempt per step.

**Wrong agent chosen** — open the task's "Plan & steps" and read the rationale with the matched keywords.
Add the missing words to the right agent's `keywords` (`PATCH /api/agents/{id}` or the Agents page). URLs,
paths and file names are intentionally ignored when routing.

**A tool call says `Invalid arguments`** — the model produced arguments that do not match the schema. The
error goes back to the agent, which normally corrects itself. Persistent cases usually mean the tool
description is unclear.

**`CONFIGURATION REQUIRED: Composio MCP …`** — honest reporting, not a bug. See
[tools.md](tools.md#composio-mcp).

**A previous attempt was interrupted (outcome unknown)** — a worker died during a mutating tool call. Atlas
refuses to repeat it automatically. Check the real state, then retry the task
(`POST /api/tasks/{id}/retry`), which acknowledges the interrupted call.

**A task will not cancel** — it should cancel immediately, even mid-execution. If the status stays
`CANCELLED` but the journal still gains events for a moment, that is the current step finishing its
transaction; no result is written.

## Tools and MCP

**No tools in the registry** — `mcp-local` is down or unreachable.
`docker compose logs mcp-local`, then `POST /api/tools/refresh`. Inside Compose `LOCAL_MCP_URL` must be
`http://mcp-local:8765/mcp`.

**`mcp_local: UNREACHABLE` with a 401** — `LOCAL_MCP_TOKEN` differs between the `api`/`worker` and
`mcp-local` containers. They read the same `.env`, so restart all of them after changing it.

**An agent cannot see a tool** — tool ids are matched with glob patterns. Check `GET /api/tools` for the
exact id and the agent's `tools` list, and make sure the tool is `enabled`.

**File tools cannot find a file** — they only see the `workspace` volume:

```bash
docker compose exec mcp-local ls /workspace
docker compose cp ./my-repo mcp-local:/workspace/my-repo
```

**`http_fetch` refuses a URL** — private, loopback and link-local addresses are blocked by design. For a
local documentation server, set `LOCAL_MCP_ALLOW_PRIVATE_FETCH=true` and restart `mcp-local`.

## Memory and learning

**Memory search returns nothing** — memories are created by consolidation after tasks finish, so a fresh
install has none. Check `GET /api/memory/stats`. With lexical embeddings, matching is by wording: search
with words that appear in the memory, or lower `MEMORY_MIN_SCORE_HASH`.

**A lesson is not being applied** — confirm it is `APPROVED` and its memory is `ACTIVE`
(`GET /api/learning/lessons`), then search for the task's wording in Memory. Lessons deprecate themselves
after 3 uses with a success rate below 34%. Project scope also applies: a lesson stored in another project
is not retrieved.

**Feedback produced no candidate** — praise alone creates none by design. Phrase it as guidance: *"Next
time, check X before Y."*

**Wrong lesson got stored** — deprecate it (Learning page or
`POST /api/learning/lessons/{id}/deprecate`). To require approval for everything, set
`LEARNING_AUTO_APPROVE_THRESHOLD=1.1`.

## Web UI

**"backend not ready" in the sidebar** — the API is not reachable from the browser.
`curl -s localhost:8000/ready`, then `docker compose logs api`.

**`502` on `/api/*` through the UI** — nginx could not reach the `api` container. It re-resolves DNS every
10 s, so this clears itself shortly after `api` restarts; otherwise `docker compose restart frontend`.

**`401` in the browser** — the proxy did not inject the token. Ensure `ATLAS_API_TOKEN` is in `.env`, then
`docker compose up -d frontend` (it is read at container start). With `npm run dev`, paste the token on the
Settings page instead.

**`403 forbidden_origin` on writes** — you are calling the API from an origin that is not in
`CORS_ORIGINS`. Add it and restart `api`.

**The UI loads but a page is empty** — check the browser console and `docker compose logs api`. Every list
endpoint returns `{items, total, …}`; an empty list means no data yet, not an error.

## Scheduler and webhooks

**A schedule never fires** — it must be `enabled` with a `next_run_at` in the past or near future, and the
worker must be running. `GET /api/schedules` shows both. `run_at` is interpreted in the schedule's
timezone. Use `POST /api/schedules/{id}/run-now` to test immediately.

**A webhook returns `401`** — with a secret set, the HMAC signature must match the **raw** body; without a
secret, the API token is required. Webhooks are not reachable through the UI proxy (port 3000) by design —
send them to port 8000.

**A webhook returned `duplicate: true`** — the same `event_id` was already processed. That is idempotency
working; use a new event id.

## Performance and resources

- **Tasks run one at a time** — raise `WORKER_CONCURRENCY` (default 2). Each task holds a database
  connection, so also consider `DB_POOL_SIZE`.
- **Long tasks lose their lease** — raise `TASK_LEASE_SECONDS` (default 120). Heartbeats renew it every
  third of that interval.
- **Prompts too large / model context errors** — lower `CONTEXT_MAX_CHARS`, `MEMORY_TOP_K` or
  `AGENT_MAX_ITERATIONS`.
- **Disk filling up** — logs are capped (10 MB × 3 per service). The database grows with journal events and
  memories; inspect with
  `docker compose exec -T postgres psql -U atlas -d atlas -c "\dt+"`.

## Data, backup and reset

```bash
# backup
docker compose exec -T postgres pg_dump -U atlas atlas | gzip > atlas-backup.sql.gz
# restore into a fresh stack
gunzip -c atlas-backup.sql.gz | docker compose exec -T postgres psql -U atlas -d atlas
# full reset (DELETES EVERYTHING: tasks, memory, lessons, workspace)
docker compose down -v && docker compose up -d
```

## Getting useful detail

Logs are JSON, one line per event, with `correlation_id`, `task_id`, `agent_id`, `status` and `duration_ms`:

```bash
docker compose logs --no-log-prefix worker | grep '"task_id": "TASK-2026-000042"'
docker compose logs --no-log-prefix api | grep '"level": "ERROR"'
LOG_LEVEL=DEBUG LOG_JSON=false docker compose up -d api worker     # readable, verbose
```

Every API response carries `X-Correlation-ID`, which ties a request to its log lines. Secrets are redacted
in logs, journal metadata and stored tool arguments, so logs are safe to share.

Reconstruct any task completely:

```bash
curl -s localhost:8000/api/tasks/TASK-2026-000042/events -H "Authorization: Bearer $ATLAS_API_TOKEN" \
  | python3 -c "import json,sys; [print(e['timestamp'][11:19], e['event_type'], (e['message'] or '')[:100]) for e in json.load(sys.stdin)]"
```
