# Setup

## Requirements

- Docker with Compose v2 (`docker compose version`). Podman 5 with its Docker-compatible socket and the
  Compose v2 CLI also works: that is how this stack was verified.
- ~8 GB free RAM and ~10 GB disk. The `omniroute` (≈4 GB) and `letta` (≈1.9 GB) images are the large ones.
- Linux, macOS or Windows with WSL2. Everything binds to `127.0.0.1`.

## 1. Configure

```bash
cp .env.example .env
```

Set these three in `.env` before the first start:

```bash
ATLAS_API_TOKEN=...       # python3 -c "import secrets; print(secrets.token_urlsafe(32))"
APPLICATION_SECRET=...    # same generator
POSTGRES_PASSWORD=...     # must be set before the first start (it creates the database volume)
```

`.env` is git-ignored. Every other setting has a working default and is documented in `.env.example`.

## 2. Start

```bash
docker compose up -d      # first run builds the backend and frontend images (a few minutes)
docker compose ps         # all services should be running/healthy
```

The `migrate` service waits for PostgreSQL, applies the migrations and exits — `Exited (0)` is expected.

| URL | What |
|---|---|
| http://localhost:3000 | Web UI |
| http://localhost:8000/docs | OpenAPI docs (`/health`, `/ready`) |
| http://localhost:20128 | OmniRoute dashboard |

Check readiness: `curl -s localhost:8000/ready` should return
`{"status":"ready","database":{"status":"OK","migration":"0001","pgvector":"0.8.6"}}`.

Atlas is usable right now. Without a model provider, agents run in labelled offline mode
(see [troubleshooting](troubleshooting.md#everything-says-offline-mode)).

## 3. Connect a model provider (OmniRoute)

This is what turns on real LLM reasoning.

1. Open http://localhost:20128 and connect at least one provider (its dashboard walks you through it).
   OmniRoute's bundled free providers are unreliable — use a provider you have credentials for.
2. Create an API key in the same dashboard.
3. In `.env`:

```bash
OMNIROUTE_ENABLED=true            # explicit opt-in: nothing is sent to the gateway until this is true
OMNIROUTE_API_KEY=<key>
MODEL_DEFAULT=<model or combo>    # e.g. a model id the dashboard lists
MODEL_FAST=<cheap/fast model>     # feedback parsing
MODEL_REASONING=<strong model>    # planning, synthesis, research and AWS agents
MODEL_CODING=<coding model>       # coding agent
EMBEDDING_MODEL=<embedding model> # optional; empty = built-in local-hash-v1 lexical embeddings
```

4. `docker compose up -d api worker` and check `/api/integrations` — `omniroute` should be `OK` with the
   model list. Leaving `MODEL_*` empty falls back to `MODEL_DEFAULT`.

Changing `EMBEDDING_MODEL` later is safe: each vector records the model that produced it, and searches only
compare vectors from the same model. Changing `EMBEDDING_DIMENSIONS` after data exists is not — the column
has a fixed width, so you would need a new migration and a re-embed.

## 4. Optional integrations

- **Composio MCP** (GitHub, AWS, Google): [tools.md](tools.md#composio-mcp)
- **Telegram bot**: [telegram.md](telegram.md)
- **Webhooks**: set `GITHUB_WEBHOOK_SECRET` / `CUSTOM_WEBHOOK_SECRET` to require HMAC signatures
- **Letta**: on by default. Set `LETTA_ENABLED=false` to use the local `sessions` table instead.

## 5. Verify end to end

```bash
scripts/demo.sh
```

It runs the Section-49 demo inside the `api` container: task → orchestrator → agent → tools → result →
memory → feedback → learning candidate → approval → a similar task reusing the lesson. It prints
`PASS`/`FAIL` per check and lists anything that is `CONFIGURATION REQUIRED`.

Browser walkthrough with screenshots (needs Playwright):

```bash
pip install playwright && playwright install chromium
UI_URL=http://localhost:3000 SHOTS_DIR=./shots python scripts/ui_demo.py
```

## Everyday commands

```bash
docker compose ps
docker compose logs -f api worker          # JSON logs, follow
docker compose restart worker
docker compose down                        # stop; data stays in volumes
docker compose down -v                     # stop and DELETE all data
docker compose up -d --build               # rebuild after code changes
docker compose exec -T postgres psql -U atlas -d atlas -c '\dt'
```

Volumes: `atlas_pgdata` (database), `atlas_workspace` (files the agents may touch),
`atlas_omniroute-data` (gateway config).

## Development outside Docker

```bash
cd backend
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e '.[dev]'    # or python -m venv + pip
cd .. && scripts/dev-db.sh start          # PostgreSQL + pgvector on localhost:5432
cd backend && .venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload   # API on :8000
.venv/bin/python -m app.worker            # worker (separate terminal)
.venv/bin/python -m app.integrations.mcp.local_server   # local MCP server on :8765
```

The backend reads the repo-root `.env` when started from `backend/`. Point `DATABASE_URL`,
`LOCAL_MCP_URL=http://localhost:8765/mcp`, `LETTA_BASE_URL` and `OMNIROUTE_BASE_URL` at `localhost` for
this mode. `ATLAS_ENV_FILE=""` disables dotenv loading entirely.

Frontend:

```bash
cd frontend && npm install && npm run dev    # http://localhost:5173, proxies /api to :8000
```

With a token-protected API, paste the token once on the UI's Settings page (it is stored in that browser
only; under Compose the nginx proxy injects it instead).

## Tests

```bash
scripts/with-test-db.sh bash -c 'cd backend && .venv/bin/pytest -q'    # 65 tests
```

The script starts a throwaway PostgreSQL + pgvector container, exports `DATABASE_URL` and removes it
afterwards. Tests also start a real local MCP server in-process. They never read your `.env`.

```bash
cd backend && .venv/bin/ruff check app tests            # lint
cd ../frontend && npm run typecheck && npm run build    # TypeScript + production build
```

## Upgrading

```bash
git pull
docker compose up -d --build   # migrate re-runs `alembic upgrade head` automatically
```

New migrations: `cd backend && .venv/bin/alembic revision --autogenerate -m "description"`, then review
the generated file (autogenerate does not detect everything).
