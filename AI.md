# Build log

How Atlas was built, why it is designed this way, and every bug that was found and fixed along the way.
Written for someone who wants to understand or continue the work, not as marketing.

- **Built by:** Kiro (Claude Opus 5.5 / 5), one session, from an empty starting point.
- **Result:** 148 files, ~14,000 lines. 65 automated tests. The full 9-service Docker Compose stack was
  started and exercised, including a real LLM run.
- **Environment note:** verification ran in a Linux sandbox using **Podman 5.2 via its Docker-compatible
  API with the Docker Compose v2.39 CLI** (Docker itself was unavailable). The compose file is standard.

---

## 1. Order of work, and why

The build followed the dependency order, not the feature list, because each layer had to be *provable*
before the next one could rely on it.

| # | Step | Why here |
|---|---|---|
| 1 | Probe the environment | Container runtime, RAM, network and image availability decide what is even possible. Done first to avoid designing something unrunnable. |
| 2 | Config, logging, errors, enums | Everything imports these. Secret redaction had to exist before anything could log. |
| 3 | Data model + migration | The schema is the contract. Written and applied against a real PostgreSQL + pgvector container before any logic. |
| 4 | Task service + journal | The single entry point and the append-only history every later layer writes to. |
| 5 | Model client, embeddings, MCP client | External edges, each with an explicit "not configured" path, so later layers never assume credentials. |
| 6 | Tool registry / executor / normalizer | Security boundary (risk, approval, recording) before any agent could call anything. |
| 7 | Agents + runner + planner + orchestrator | The actual pipeline, built on top of parts that already worked. |
| 8 | Memory, consolidation, learning, reports | Value on top of a working pipeline. |
| 9 | Worker, scheduler, webhooks, Telegram | Interfaces that only create tasks. |
| 10 | Tests | Written next to each layer, then run as a whole against a real database. |
| 11 | Frontend | Last, because it only consumes the API. |
| 12 | Compose stack + live verification | Prove the deliverable, not the source files. |

**Rule followed throughout:** no layer was considered done until something executed it. Several designs
changed *because* the run disagreed with the design (section 3).

---

## 2. Design decisions and the reasoning

### 2.1 PostgreSQL is the queue — no broker
`SELECT … FOR UPDATE SKIP LOCKED` with a lease column and heartbeats gives exactly the needed semantics:
one worker per task, automatic recovery when a worker dies, and task state that is already durable because
it is in the same transaction as the work. Adding Redis or RabbitMQ would mean two sources of truth to
reconcile after a crash, for a single-machine system that never needs that throughput.

### 2.2 One pipeline, enforced structurally
Telegram, the scheduler and webhooks do not have "their own" execution path — they call
`tasks.service.create_task()` (or `POST /api/tasks`) and stop there. The worker is the only process that
executes tasks. This is not a convention; there is simply no other code path that runs an agent.

### 2.3 Stage-per-transaction, not one big transaction
The orchestrator commits after prepare, after planning, after each step and at finalize. A long
transaction would hold locks across minutes of LLM latency and lose all progress on a crash. With
stage-per-transaction, a restart never repeats a finished stage, and a paused step resumes from its
committed checkpoint.

### 2.4 Tier-based model routing
Agents ask for a *tier* (`fast`, `reasoning`, `coding`, `default`), never a provider or model name. The
mapping lives in `.env`. Swapping every model in the system is a config change, and no agent code contains
a provider name.

### 2.5 Offline mode instead of failing, and instead of faking
With no model reachable, a naive system either crashes or invents output. Atlas does a third thing: a
deterministic engine ranks the agent's tools by word overlap with the request, executes only those whose
required arguments can be *derived* from the request itself, and assembles a report from real tool results
and real retrieved memory. Every such result carries `offline: true` and says why. It never invents tool
output, resource names or metrics.

### 2.6 Letta for session state only
Letta manages the conversation/session layer (memory blocks per `session_key`). Long-term semantic,
episodic, procedural and evidence memory is Atlas' own pgvector store, because it needs project scoping,
importance/confidence ranking, consolidation rules and a learning pipeline. Running two competing
long-term memory systems would mean two answers to "what do we know". If Letta is down, the local
`sessions` table takes over and the result records which runtime was used.

### 2.7 Two embeddings per memory, always
Every memory gets a `local-hash-v1` lexical embedding (deterministic feature hashing, no model, no
network) and, when configured, a gateway embedding. Each vector records the model that produced it and
searches only compare like with like. Consequence: memory works on day one with zero credentials, and
switching embedding models later does not corrupt old data.

### 2.8 Risk is a property of the tool, approval is a property of the task
Risk is read from MCP annotations first, then from conservative name heuristics (anything that looks like a
mutation is at least WRITE). `DESTRUCTIVE` always requires approval, and the API cannot relax that. The
task — not the tool call — is what pauses, so the pause is visible, durable and resumable from any
interface.

### 2.9 Learning may only write memory
The learning engine writes to exactly four places: memories, lessons, skills, and the retrieval/planning
context. There is no code path from feedback to source code, agent instructions, security settings, tool
risk levels or the schema. Editing an agent is a separate, audited operator action. This was a constraint
on the design, not a check bolted on afterwards.

### 2.10 Honesty as a feature
Missing integrations surface as `CONFIGURATION REQUIRED` in the task result, the journal, `/api/integrations`
and the UI. A system that silently degrades to plausible-sounding output is worse than one that says what
it cannot do.

---

## 3. Bugs and design errors found, and how they were fixed

Every item here was found by running something, not by reading code. This is the honest list.

### Environment and dependencies

**1. The MCP SDK had a breaking major version.**
`pip install mcp` resolved to 2.2.0, where `FastMCP` is renamed to `MCPServer` and the client API changed;
the v1 code raised `ModuleNotFoundError` with a migration hint.
*Fix:* inspected both APIs in the installed package, pinned `mcp>=1.9,<2`, and wrote against the API that
is actually installed.

**2. Comma-separated list settings silently failed.**
pydantic-settings tries to JSON-decode `list[...]` env vars, so `TELEGRAM_ALLOWED_CHAT_IDS=1,2` raised a
parse error instead of yielding `[1, 2]`.
*Fix:* `Annotated[list[int], NoDecode]` plus a `mode="before"` validator that splits on commas.

**3. Alembic autogenerate missed three things it cannot see.**
The `vector` extension, the HNSW index and the `task_id_seq` sequence are not model metadata, so the
generated migration would have failed on a fresh database.
*Fix:* added them explicitly to migration `0001` (`CREATE EXTENSION`, `CREATE SEQUENCE`, `CREATE INDEX …
USING hnsw`), with the dimension read from config and a `<= 2000` guard because HNSW has a dimension limit.

### Retrieval and routing (found by the demo giving wrong answers)

**4. Learned lessons were never retrieved — the similarity threshold was wrong for lexical embeddings.**
The end-to-end demo failed at its most important step: the follow-up task did not apply the approved
lesson. The cause was one shared `MEMORY_MIN_SCORE`. Lexical hash embeddings have a much lower natural
similarity range than model embeddings (0.14 for a clearly relevant match), so a threshold tuned for model
embeddings filtered out every correct result.
*Fix:* separate thresholds per embedding family (`MEMORY_MIN_SCORE` 0.3, `MEMORY_MIN_SCORE_HASH` 0.1),
applied to the fallback path too. *Lesson:* a retrieval threshold is a property of the embedding space, not
of the application.

**5. File paths and URLs hijacked agent routing.**
"Delete the file `notes/restart-demo.txt`" routed to the AWS/DevOps agent, because "restart" appeared in
the *filename*.
*Fix:* `strip_literals()` removes URLs, paths and file names before keyword scoring. Arguments still use
the raw text. Tests now assert this for both a path and a URL.

**6. The research agent won everything.**
Its keyword list contained generic words (`what`, `how`, `why`, `find`, `summarize`, `analyze`), so
*"Analyze today's AWS alarms"* routed to Research instead of AWS/DevOps, and the multi-intent detector
turned single-domain requests into two-agent plans.
*Fix:* trimmed the list to genuinely domain-specific words, and removed `and summarize` from the
multi-intent pattern.

**7. Offline tool selection matched on descriptions.**
Relevance was computed over name + description + capabilities, so almost every tool scored above zero and
irrelevant tools were considered.
*Fix:* score tool **name** tokens (stemmed, with generic words like `get`/`workspace`/`http` removed) plus
a smaller weight for capability tags; ignore descriptions entirely.

**8. Ties could hand a specialist request to the generalist.**
*Fix:* sort by `(score, is_specialist)` descending, and require a specialist score ≥ 1.0 before it is
chosen, otherwise the generalist takes it.

### Concurrency (found by deliberately racing the system)

**9. Cancelling a running task could be overwritten.**
The worker read the task, ran the agent for minutes, then wrote `COMPLETED` — clobbering a `CANCELLED`
written meanwhile.
*Fix:* every status transition (step start, applying a step result, finalize, retry, approval decision)
takes a row lock and re-reads the status; a cancel always wins, the work already done stays in the journal,
and pending approvals are closed. Finalize was split so the LLM synthesis call happens *outside* the lock.

**10. The row lock blocked the cancel it was supposed to protect against.**
`FOR UPDATE` on `tasks` conflicts with the `FOR KEY SHARE` locks that foreign-key inserts into
`task_events` / `tool_executions` take while an agent is running — so a user's cancel waited for the whole
agent loop.
*Fix:* `FOR NO KEY UPDATE` everywhere. Status writers still serialize against each other, but journal
inserts never block a cancel.

**11. API and worker raced while seeding.**
Both seed built-in agents, the default project and the tool registry at startup; the read-then-insert
pattern could raise unique-violations.
*Fix:* `INSERT … ON CONFLICT DO NOTHING` / `DO UPDATE` for agents, project and tools. A concurrency test
now runs four seeders in parallel.

**12. A crash could hide a tool side effect, and a retry could repeat it.**
The execution record was written *after* the provider call, so a worker killed mid-call left no trace —
and the retry would call the tool again. For a delete or a restart, that is the worst possible behaviour.
*Fix:* write-ahead recording (a `RUNNING` row plus `TOOL_CALLED` committed *before* the call), plus
`INTERRUPTED` / `ABANDONED` states, plus at-most-once semantics for mutations: a completed identical call
is replayed from its stored result, and an interrupted one is never re-run automatically — only after a
human retries the task. Verified by killing the worker with `SIGKILL` mid-call.

### Model gateway

**13. Prompts could be sent to a gateway nobody had configured on purpose.**
Having `OMNIROUTE_BASE_URL` set (it has a default in Compose) was enough to start sending prompts, and
OmniRoute's default `auto` combo can route to third-party free providers.
*Fix:* explicit `OMNIROUTE_ENABLED` opt-in. Nothing leaves the machine until it is `true`.

**14. A dead gateway cost three retries on every single call.**
With the gateway down, every model call burned its full retry budget, making tasks slow and logs noisy.
*Fix:* two attempts instead of three (OmniRoute already retries providers internally), plus a circuit
breaker that skips the gateway for a cooldown after a failure and reports the reason.

**15. Raw gateway error JSON leaked into user-facing summaries.**
Offline summaries contained a wall of provider error JSON.
*Fix:* `short_reason()` truncates at the first bracket; the full error stays in the `MODEL_CALL` journal
metadata.

**16. Token counts were redacted as secrets.**
Found by the real-LLM run: the journal showed `usage={'prompt_tokens': '***REDACTED***'}`, because the
secret-key pattern matches the word `token` and `prompt_tokens` contains it.
*Fix:* a small allow-list (`prompt_tokens`, `completion_tokens`, `total_tokens`, `max_tokens`, …) checked
before redaction, with a test asserting counts survive while `bot_token` is still redacted.

### Deployment and interfaces

**17. The UI proxy kept using a stale API container IP.**
After `docker compose restart api`, nginx returned `502` for every `/api` call, because it resolves
upstream host names once at startup.
*Fix:* `resolver` from the container's own `/etc/resolv.conf` plus a variable `proxy_pass`, so the name is
re-resolved (10 s TTL). Re-verified by restarting `api` and calling the API through the proxy.

**18. Compose ordering depended on healthchecks that are not always available.**
Podman without systemd does not run healthchecks, so `depends_on: service_healthy` never became true and
the stack did not start.
*Fix:* the `migrate` service waits for PostgreSQL itself (`app/database/migrate.py`) and every service
retries its dependencies. Healthchecks remain, but only as information.

**19. Backend tests read the developer's `.env`.**
With a dotenv file present, tests inherited real settings and could have talked to real services.
*Fix:* `ATLAS_ENV_FILE=""` in `conftest.py` disables dotenv loading; the env-file list is itself
configurable.

**20. The Playwright walkthrough clicked before the page had loaded.**
"Approve lesson" was clicked while the candidate list was still fetching, so the assertion that followed
failed.
*Fix:* explicit waits on the button and on the resulting "Lessons (1)" heading — and a failure handler that
prints one readable line instead of a full page dump.

**21. Two MCP servers in one process fought over the session manager.**
A test that starts a second local MCP server hit "can only be called once" from
`StreamableHTTPSessionManager`.
*Fix:* the test resets the cached session manager; `build_app()` keeps the auth wrapper separate so the
server is constructible more than once.

### Repository structure

**22. The work was initially placed in the wrong repository.**
The brief said "empty Git repository", but `ajeetbr21/Agent` contained an unrelated VS Code extension
(LeechCode) on `main` plus an open PR. Building there would have mixed two unrelated projects.
*Fix:* the platform moved to its own repository at root level, and `ajeetbr21/Agent` was restored to
pristine `origin/main` — nothing was ever pushed to it and the temporary branch was deleted.

---

## 4. What was verified, and how

### Automated tests — 65, all passing

```bash
scripts/with-test-db.sh bash -c 'cd backend && .venv/bin/pytest -q'
```

Against a **real** PostgreSQL 16 + pgvector container and a **real** local MCP server started in-process.
Scripted HTTP transports stand in for the model gateway, Letta and Telegram where a test needs determinism.

| Area | Examples |
|---|---|
| Database | migration applies, pgvector works, HNSW index exists, cosine distance correct |
| Tasks | id format, idempotency, pagination, cancel/retry conflicts, project auto-detection |
| API | error shape, validation, token auth, CSRF origin guard, correlation ids |
| Routing / planning | four request types route correctly, paths/URLs ignored, LLM plan accepted, invalid LLM plan rejected and replaced |
| Model client | tier → model mapping, auth header, tool-call parsing, usage, offline fallback, circuit breaker, opt-in |
| Tools | MCP discovery, risk classification, argument validation, write-ahead record, replay of completed mutations, interrupted mutations needing acknowledgement, unconfigured provider, per-agent scoping, MCP token auth |
| Approvals | destructive pause → approve → execute, reject → never executes, cancel closes approvals, retry re-asks |
| Memory | lexical embedding sanity, dedupe, ranking order, project boundaries, stats |
| Learning | parser categories/confidence, candidate → lesson → skill, duplicate reinforcement, sensitive never auto-approved, outcome tracking and auto-deprecation |
| Scheduler | ONCE fires once, DAILY/WEEKLY/MONTHLY cron derivation, invalid cron rejected, idempotency |
| Webhooks | GitHub signature + delivery dedupe, custom signature, validation |
| Telegram | commands, task follow-up, approval and feedback callbacks, unauthorized chat, real API client |
| Concurrency / recovery | cancel during an agent loop, parallel seeding, crash recovery, startup recovery |
| End to end | the Section-49 demo offline and with an LLM, multi-agent + report, approval pause/resume |

### Live Docker Compose stack

All nine services started. Verified:

| What | Result |
|---|---|
| Section-49 demo through the **real Web UI** (Playwright) | PASS — 15 screenshots, zero browser console errors |
| `scripts/e2e_demo.py` (30 checks) | ALL PASSED |
| Restart of `api` + `worker` while a task waited for approval | resumed and completed |
| `SIGKILL` on the worker mid tool call | recovered, killed attempt marked `INTERRUPTED`, task completed |
| Task queued while the worker was stopped | ran on start |
| PostgreSQL restart | services reconnected |
| Full `down` / `up` | tasks, memories and lessons all still present |
| Scheduler | fired as a normal task, produced a report, did not re-fire |
| Webhook redelivery | deduplicated |
| Auth / CSRF / proxy | no token 401, wrong token 401, foreign origin 403, foreign Host 403, webhook via UI proxy 403 |
| Letta 0.16.8 | session blocks created and read |
| Local MCP server | 9 tools discovered with token auth |

### Real LLM — verified

Verified with a local **Ollama** server (`qwen2.5:1.5b`, CPU, no API key, no signup), because the
OmniRoute container's bundled free providers refused every request (HTTP 403/400/502) and no provider
credentials were available:

```
gateway : OK | routing {default,fast,reasoning,coding} → qwen2.5:1.5b
planner : llm (model qwen2.5:1.5b)          agent: research
journal : TASK_CREATED > … > MODEL_CALL > TOOL_CALLED > TOOL_RESULT > MODEL_CALL >
          AGENT_COMPLETED > VALIDATION > STEP_COMPLETED > MEMORY_CREATED > TASK_COMPLETED
run     : research COMPLETED provider=omniroute model=qwen2.5:1.5b iterations=2 tokens=1902+64
tool    : local:get_current_time SUCCESS 23ms
offline : False
summary : "The current time in UTC is 2026-09-29T23:31:47.154166+00:00."
VERIFY_LLM: PASS
```

So the whole chain works with a real model: LLM planning → agent → OpenAI tool call → real tool execution →
real data in the answer → validation → memory. Reproduce it yourself with `scripts/verify_llm.py` (its
docstring has the exact commands).

### Still not verified

| What | Why | How to verify |
|---|---|---|
| A hosted model provider through OmniRoute | No provider credentials. The container ran and Atlas reached it, but its free providers refused. The gateway path itself is proven by the Ollama run, which uses the identical OpenAI-compatible client. | [docs/setup.md](docs/setup.md#3-connect-a-model-provider-omniroute) |
| Composio MCP against the live service | Needs a Composio account. URL/header construction, discovery, risk mapping and the `CONFIGURATION_REQUIRED` path are covered by tests. | [docs/tools.md](docs/tools.md#composio-mcp) |
| Telegram against the live Bot API | Needs a BotFather token. Handlers, commands, callbacks and the API client are covered by tests. | [docs/telegram.md](docs/telegram.md) |

---

## 5. Things deliberately not built

Out of scope by instruction, and the code contains no half-finished version of any of them: cloud
deployment (AWS/Oracle), Kubernetes, public gateways/load balancers/CDN/DNS, production TLS, managed
databases, multi-region, autoscaling.

Also deliberately avoided: a message broker (section 2.1), a second agent runtime (2.6), microservices (one
backend image, one worker), and any local model requirement (models are optional and remote by default).

---

## 6. Known limitations

- **Lexical fallback embeddings match wording, not meaning.** Without `EMBEDDING_MODEL`, "restart the
  server" and "reboot the host" do not match well. Set a real embedding model for semantic recall.
- **Small models are weak planners.** The 1.5B model used for verification plans simply and sometimes skips
  tools. Point `MODEL_REASONING` at a stronger model for real work.
- **Steps run sequentially.** Independent steps of one task could run in parallel; they do not. Different
  tasks do run concurrently (`WORKER_CONCURRENCY`).
- **Single-node by design.** Several workers on one database work (the queue uses `SKIP LOCKED`), but there
  is no cross-host coordination beyond leases.
- **Reports are Markdown + JSON**, not PDF.
- **One migration.** The schema is `0001`; future changes need new revisions.
- **The UI polls** (2–5 s) instead of using websockets. Simple and adequate locally.

---

## 7. Where to look in the code

| Question | File |
|---|---|
| How does a task get created and queued? | `backend/app/tasks/service.py` |
| What is the pipeline? | `backend/app/orchestrator/orchestrator.py` |
| How is an agent chosen? | `backend/app/agents/registry.py`, `orchestrator/planner.py` |
| How does the tool loop work? | `backend/app/agents/runner.py` |
| What happens without an LLM? | `backend/app/agents/heuristics.py` |
| How are tool calls secured and recorded? | `backend/app/tools/executor.py` |
| How is memory ranked? | `backend/app/memory/service.py` |
| What decides if something is remembered? | `backend/app/memory/consolidation.py` |
| How does feedback become a lesson? | `backend/app/learning/engine.py` |
| How does recovery work? | `backend/app/worker.py`, `tasks/service.py::recover_stale_tasks` |
| What is the schema? | `backend/alembic/versions/0001_initial_schema.py` |

Architecture in prose: [docs/architecture.md](docs/architecture.md).
