# API

Local control plane on `http://localhost:8000`. Interactive docs: `/docs` (OpenAPI schema at
`/openapi.json`). Everything the Web UI and the Telegram bot do goes through these endpoints.

## Authentication

`/api/*` requires `ATLAS_API_TOKEN` when it is set, as either header:

```bash
curl -s localhost:8000/api/tasks -H "Authorization: Bearer $ATLAS_API_TOKEN"
curl -s localhost:8000/api/tasks -H "X-API-Key: $ATLAS_API_TOKEN"
```

`/health`, `/ready` and `/docs` are open. An empty token disables auth, which is only acceptable because the
port binds to `127.0.0.1`. Browser requests that change state must come from an origin in `CORS_ORIGINS` and
must be `application/json` (CSRF protection).

## Errors

```json
{"error": {"code": "not_found", "message": "Task TASK-2026-000001 not found",
           "details": null, "correlation_id": "a1b2c3d4e5f6"}}
```

`validation_error` (422), `not_found` (404), `conflict` (409), `unauthorized` (401), `forbidden_origin`
(403), `unsupported_media_type` (415), `configuration_required` (503), `internal_error` (500). Every
response also carries an `X-Correlation-ID` header, which appears in the logs and on the task.

List endpoints return `{"items": [...], "total": n, "limit": n, "offset": n}`.

## Tasks

| Method | Path | Notes |
|---|---|---|
| POST | `/api/tasks` | create a task — the single entry point for every source |
| GET | `/api/tasks` | `status` (comma-separated), `source`, `q`, `limit`, `offset` |
| GET | `/api/tasks/{id}` | task with steps and session state |
| GET | `/api/tasks/{id}/events` | the task journal; `after_id` for incremental polling |
| GET | `/api/tasks/{id}/tool-executions` | every tool call with status, duration, redacted arguments |
| GET | `/api/tasks/{id}/agent-runs` | attempts per step: model, provider, iterations, tokens |
| GET | `/api/tasks/{id}/report` | structured report, or `null` |
| POST | `/api/tasks/{id}/cancel` | takes effect even mid-execution; closes pending approvals |
| POST | `/api/tasks/{id}/retry` | `FAILED`/`CANCELLED` only; completed steps are kept |

```bash
curl -s localhost:8000/api/tasks -H "Authorization: Bearer $ATLAS_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "request": "Analyze today'\''s AWS alarms and summarize anything that needs attention.",
    "source": "api", "priority": 5,
    "user_external_id": "me", "session_key": "api:me",
    "idempotency_key": "alarms-2026-09-29"}'
```

`source`: `web|api|telegram|scheduler|webhook`. `priority`: 1 (most urgent) to 9. `project` selects a
project by slug; omit it for keyword auto-detection. `idempotency_key` is unique — repeating it returns the
existing task with `"created": false`. `parent_task_id` links a subtask.

Statuses: `PENDING → PLANNING → RUNNING → COMPLETED`, plus `WAITING_APPROVAL`, `RETRYING`, `FAILED`,
`CANCELLED`. Poll `/api/tasks/{id}` or stream the journal with `after_id`.

The `result` object holds `summary`, `findings`, `recommendations`, `offline`, `validation`,
`configuration_required`, `applied_lessons`, `steps[]`, `report_id` and `memory` counts.

## Agents and tools

| Method | Path | Notes |
|---|---|---|
| GET | `/api/agents` | `include_disabled` (default true) |
| GET | `/api/agents/{id}` | one agent |
| PATCH | `/api/agents/{id}` | audited operator edit: `instructions`, `capabilities`, `keywords`, `tools`, `model_preference`, `status` |
| GET | `/api/agents/runs` | recent agent runs across tasks |
| GET | `/api/tools` | the tool registry with risk levels |
| POST | `/api/tools/refresh` | re-discover tools from all MCP providers |
| PATCH | `/api/tools/{id}` | `enabled`, `requires_approval` (cannot be relaxed for `DESTRUCTIVE`) |
| GET | `/api/tools/executions` | recent tool calls |

## Approvals

| Method | Path | Notes |
|---|---|---|
| GET | `/api/approvals` | `status=PENDING`, `task_id` |
| GET | `/api/approvals/{id}` | one approval |
| POST | `/api/approvals/{id}/decision` | `{"approve": true, "decided_by": "me", "channel": "web", "reason": null}` |

Approving re-queues the task, which resumes from its checkpoint. A second decision returns 409.

## Memory

| Method | Path | Notes |
|---|---|---|
| GET | `/api/memory` | `type`, `project`, `status`, `q`, pagination |
| POST | `/api/memory` | add knowledge: `type`, `content`, `title`, `tags`, `project`, `confidence`, `importance` |
| GET | `/api/memory/stats` | active records per type |
| POST | `/api/memory/search` | `{"query": "...", "project": null, "types": null, "limit": 8}` → hits with `score` and `similarity` |
| GET/PATCH | `/api/memory/{id}` | read; update `status`, `importance`, `confidence` |

## Feedback and learning

| Method | Path | Notes |
|---|---|---|
| POST | `/api/feedback` | `task_id`, `content`, optional `rating` → returns the learning candidates it produced |
| GET | `/api/feedback` | `task_id`, `limit` |
| GET | `/api/learning/candidates` | `status=CANDIDATE` |
| POST | `/api/learning/candidates/{id}/decision` | `{"approve": true, "edited_lesson": "...", "decided_by": "me"}` |
| GET | `/api/learning/lessons` | approved lessons with usage and success counts |
| POST | `/api/learning/lessons/{id}/deprecate` | stop retrieving a lesson |
| GET/POST | `/api/learning/skills` | list, or define a procedure directly |
| GET | `/api/learning/summary` | counts per status, lessons, skills, ratings |

## Reports, schedules, projects

| Method | Path | Notes |
|---|---|---|
| GET | `/api/reports` · `/api/reports/{id}` | structured reports |
| GET | `/api/reports/{id}/markdown` | rendered Markdown (`text/plain`) |
| GET | `/api/schedules` | all schedules with `next_run_at` |
| POST | `/api/schedules` | see below |
| PATCH | `/api/schedules/{id}` | `enabled`, `request`, `name` |
| DELETE | `/api/schedules/{id}` | 204 |
| POST | `/api/schedules/{id}/run-now` | fire immediately as a normal task |
| GET/POST | `/api/projects` | list, or create with `slug`, `name`, `keywords` |

```bash
# every weekday at 08:00 UTC
curl -s localhost:8000/api/schedules -H "Authorization: Bearer $ATLAS_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name": "morning alarms", "request": "Summarize AWS alarms from the last 24 hours",
    "schedule_type": "CRON", "cron_expression": "0 8 * * 1-5", "timezone": "Europe/Berlin"}'
```

`schedule_type`: `ONCE` (needs `run_at`), `DAILY`, `WEEKLY`, `MONTHLY` (use `run_at` as the anchor time) or
`CRON` (5-field expression). Each firing creates a normal task with a unique idempotency key, so a
duplicate tick can never run it twice.

## Webhooks

| Method | Path | Notes |
|---|---|---|
| POST | `/api/webhooks/github` | needs `X-GitHub-Event` and `X-GitHub-Delivery` |
| POST | `/api/webhooks/custom` | `{"event_id": "...", "request": "...", "project": null, "priority": 5, "payload": {}}` |

Both return `202` with `{"event_id", "task_id", "duplicate"}`. Redelivery of the same event id returns the
same task with `duplicate: true` — it never runs twice.

Authentication: with a secret set (`GITHUB_WEBHOOK_SECRET`, `CUSTOM_WEBHOOK_SECRET`) an HMAC-SHA256
signature is required (`X-Hub-Signature-256`, `X-Atlas-Signature`); otherwise the API token is required.
GitHub `push`, `pull_request`, `issues` and failed `workflow_run` events are turned into meaningful task
requests; `ping` is acknowledged without creating a task. These endpoints are deliberately not reachable
through the UI proxy.

```bash
BODY='{"event_id":"backup-2026-09-29","request":"Nightly backup failed on db-1, investigate"}'
SIG="sha256=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$CUSTOM_WEBHOOK_SECRET" -r | cut -d' ' -f1)"
curl -s localhost:8000/api/webhooks/custom -H 'Content-Type: application/json' \
  -H "X-Atlas-Signature: $SIG" -d "$BODY"
```

## System

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness, uptime |
| GET | `/ready` | 200 when the database, migration revision and pgvector are all present, else 503 |
| GET | `/api/integrations` | per-integration status: `OK`, `CONFIGURATION_REQUIRED`, `UNREACHABLE`, `DISABLED` |
| GET | `/api/system/dashboard` | task counts, recent tasks/runs/tool calls/learning, memory stats, system events |
