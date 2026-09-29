# Architecture

## Services (docker-compose.yml)

| Service | Image | Role |
|---|---|---|
| `postgres` | pgvector/pgvector:pg16 | Only database: tasks/queue, journal, memory + vectors, learning, schedules. Also holds a separate `letta` database. |
| `migrate` | atlas-backend | Waits for PostgreSQL, runs `alembic upgrade head`, exits. |
| `api` | atlas-backend | FastAPI control plane on `127.0.0.1:8000`. Creates tasks, never executes them. |
| `worker` | atlas-backend | Runs tasks through the orchestrator, fires schedules, recovers interrupted tasks, refreshes tools. |
| `mcp-local` | atlas-backend | Atlas' own MCP server (Streamable HTTP, `:8765/mcp`) with local tools, confined to the `workspace` volume. |
| `telegram` | atlas-backend | Long-polling bot. Talks only to the API, never to the database. |
| `frontend` | atlas-frontend | nginx serving the React dashboard on `127.0.0.1:3000` and proxying `/api` with the API token. |
| `omniroute` | diegosouzapw/omniroute | OpenAI-compatible model gateway on `127.0.0.1:20128`. |
| `letta` | letta/letta:0.16.8 | Session-state runtime (memory blocks). Not published to the host. |

There is a single backend image and one worker process with bounded concurrency (`WORKER_CONCURRENCY=2`).
There is no message broker: PostgreSQL is the queue (`SELECT … FOR NO KEY UPDATE SKIP LOCKED`).

## One pipeline for every source

```
source (web | api | telegram | scheduler | webhook)
  └─ tasks.service.create_task()          id TASK-<year>-<seq>, idempotency key, project detection
       └─ worker claims (lease + heartbeat)
            └─ Orchestrator.run(task_id)
                 1 prepare    normalize request · project · retrieve memory · load session (Letta)
                 2 plan       LLM planner (reasoning tier) or rule-based router → task_steps
                 3 steps      per step: AgentRunner loop (model ⇄ tools) → validate → retry once if invalid
                 4 finalize   task-level validation · synthesis · report · memory consolidation · session update
```

Telegram, the scheduler and webhooks only call `create_task` (directly or via `POST /api/tasks`). None of
them has its own execution path.

### Orchestrator (`app/orchestrator/orchestrator.py`)
It coordinates agents through two interfaces, `AgentRegistry` (discovery and routing) and `AgentRunner`
(execution). It contains no agent-specific code. Each stage runs in its own database transaction, so a
restart never repeats a finished stage:

- **prepare** stores the normalized request, the retrieved memory context and the session conversation in
  `tasks.session` (session memory).
- **plan** writes one `task_steps` row per step. Plans are limited to 5 steps. Dependencies may only point
  to earlier steps, so agent-to-agent loops cannot be planned.
- **steps** run in order. Completed steps are never re-run. A step paused for approval resumes from its
  committed checkpoint (the agent's message history).
- **finalize** runs synthesis (which may call the LLM) outside the row lock, then commits the result under
  the lock.

### Planner (`app/orchestrator/planner.py`)
- **LLM planner.** It sends the request, the agent catalogue, routing scores and learned procedures to the
  *reasoning* model and asks for JSON. The plan is validated: agents must exist and be active, there are
  at most 5 steps, and dependencies must point backwards. An invalid plan falls back to the rule-based
  planner.
- **Rule-based planner.** It scores agents by keyword and capability overlap. URLs, paths and file names
  are ignored, so `notes/restart.txt` does not trigger the AWS agent. Multi-intent phrases ("…, research
  the documentation, and prepare a report") or two strong specialists produce a multi-agent plan, ordered
  AWS/DevOps → Coding → Research → General. Later steps receive earlier results.
- A report is generated for multi-step plans, report-style requests and scheduled tasks.

### Validator (`app/orchestrator/validator.py`)
A step is invalid when it has no summary; that triggers one re-run. These produce warnings instead:
failed or blocked tool calls, `CONFIGURATION REQUIRED` gaps, and offline mode. Confidence goes up when all
tool calls succeed and down for each failed one.

## Concurrency and consistency

- **Row locks on status changes.** Cancel, retry, approval decisions, step start, applying a step result
  and finalize all lock the task row with `SELECT … FOR NO KEY UPDATE` and re-read the status. A task
  cancelled while its agent is still running stays `CANCELLED`. The work already done stays in the journal,
  and pending approvals are closed.
- **Idempotent writes.** Agent seeding, default-project creation and tool discovery use
  `INSERT … ON CONFLICT`, so the API and the worker can start at the same time.
- **Write-ahead tool calls.** A `tool_executions` row with status `RUNNING` and a `TOOL_CALLED` event are
  committed *before* the provider is called, and the result is committed right after. A crash therefore
  cannot hide that a tool may have run, and progress shows up live in the UI.
- **Side effects happen at most once.** For WRITE/DESTRUCTIVE tools, a call already completed in the same
  task with the same arguments is replayed from its recorded result instead of running again. A call left
  `INTERRUPTED` (the worker died, so the outcome is unknown) is not re-run automatically. It is re-run only
  after a human retries the task, which marks it `ABANDONED`.

## Recovery

| Situation | What happens |
|---|---|
| Worker killed (SIGKILL, OOM, power loss) | Leases expire after `TASK_LEASE_SECONDS` (120 s). At startup the worker immediately recovers tasks held by its own host's previous process. Tasks go to `RETRYING` (`retry_count`+1, budget `TASK_MAX_RETRIES`). `RUNNING` agent runs and tool calls become `INTERRUPTED`. The step restarts from its last committed checkpoint. |
| Waiting for approval during a restart | Nothing to recover. The state (`WAITING_APPROVAL`, approval row, checkpoint) is in PostgreSQL, and approving re-queues the task. |
| API/worker restart with queued tasks | Tasks stay `PENDING` and are claimed once the worker is up. |
| PostgreSQL restart | Pooled connections are pre-pinged. The worker loop catches errors and retries. |
| A step raises an exception | Retried once (`MAX_STEP_ATTEMPTS=2`) within the task retry budget, then the task is `FAILED` with the error recorded. |

## Model gateway (`app/integrations/omniroute/client.py`)
All LLM calls go agent → `ModelClient` → OmniRoute `/v1/chat/completions` (OpenAI tool-calling format).
Models are chosen by **tier**: `fast` for feedback parsing, `reasoning` for planning, synthesis and the
research and AWS agents, `coding` for the coding agent, and `default` for the rest. Each tier maps to
`MODEL_*` in `.env`, so no agent depends on a provider.

- **Explicit opt-in.** Nothing is sent to the gateway unless `OMNIROUTE_ENABLED=true`.
- **Retries and circuit breaker.** A failed call is retried once, then the gateway is skipped for
  `OMNIROUTE_FAILURE_COOLDOWN_SECONDS`.
- **Offline fallback.** With `LLM_OFFLINE_FALLBACK=true` every caller falls back to its deterministic path,
  and the `MODEL_CALL` journal event records why.

## Session state (Letta) vs long-term memory
- **Session memory is temporary.** It covers the current conversation, task, plan, intermediate step
  results and tool outputs. Per task it lives in `tasks.session`. Per conversation (`session_key`, e.g.
  `telegram:<chat>` or `web:<user>`) it lives in three Letta memory blocks: `conversation`, `active_task`
  and `scratchpad`. If Letta is disabled or unreachable, the local `sessions` table is used instead and a
  warning is recorded. Atlas does not run a second agent runtime.
- **Long-term memory** is Atlas' own: `memories` + `memory_embeddings`. Session data only becomes long-term
  memory through consolidation ([memory.md](memory.md)).

## Context window
The model receives the agent instructions, the step objective and the original request, results of the
steps this step depends on, the recent conversation (≤ 2.5k chars), the retrieved memories (top-k) and the
planning guidance. The whole context is capped at `CONTEXT_MAX_CHARS`. Inside a running loop, tool outputs
are cut to 6k chars. Once the budget is exceeded, older tool outputs are shortened to 600 chars (only the
last two stay intact) and a `CONTEXT_COMPACTED` event is written.

## Data model (migration `0001`)
`users`, `projects`, `tasks`, `task_events` (journal), `task_steps`, `agents`, `agent_runs`, `tools`,
`tool_executions`, `approvals`, `memories`, `memory_embeddings` (pgvector, HNSW cosine index),
`sessions`, `feedback`, `learning_candidates`, `lessons`, `skills`, `reports`, `scheduled_tasks`,
`webhook_events`, `system_events`, `audit_logs`. All tables have foreign keys, timestamps and indexes on the
columns used for lookups. Idempotency is enforced by unique constraints: `tasks.idempotency_key`,
`webhook_events (source, event_id)` and `reports.task_id`.

## Observability
- **Logs.** JSON lines on stdout with `timestamp`, `level`, `logger`, `event`, `correlation_id`, `task_id`,
  `agent_id` and fields such as `status`, `duration_ms`, `tool_id` and `model`. Secret-looking keys and
  values (tokens, keys, passwords, Bearer headers, AWS key ids, Telegram tokens) are redacted in logs,
  journal metadata and stored tool arguments. Compose rotates logs at 10 MB × 3 files.
- **Correlation ids.** Every HTTP response carries `X-Correlation-ID`, and the task stores it.
- **Health endpoints.** `/health` is liveness. `/ready` checks the database, the migration revision and
  pgvector. `/api/integrations` reports each integration, and `/api/system/dashboard` feeds the dashboard.
- **Audit trail.** Approvals, learning decisions and operator edits go to `audit_logs`. Worker and scheduler
  events go to `system_events`.

## Security boundaries
- **Network.** Every published port binds to `127.0.0.1`. `/api/*` requires `ATLAS_API_TOKEN` when it is
  set (as a Bearer token or `X-API-Key`).
- **CSRF.** Writes from a browser origin not listed in `CORS_ORIGINS` get 403, and request bodies must be
  `application/json`.
- **UI proxy.** nginx injects the token server-side, answers only localhost host names (DNS-rebinding
  protection) and does not proxy webhooks.
- **Webhooks.** They require an HMAC signature when a secret is set, otherwise the API token.
- **Local MCP server.** Files are confined to the workspace volume, `http_fetch` refuses private and
  loopback addresses, and `LOCAL_MCP_TOKEN` can require a shared secret.
- **Containers.** Backend containers run as a non-root user.
- **No self-modification.** Learning writes only memory, lessons and skills. It cannot change code, agent
  instructions, security settings or the schema ([learning.md](learning.md)).
