# Tools and MCP

Every tool call takes the same path:

```
agent → Tool Registry (which tools may I use?) → ToolExecutor (validate · approve · record)
      → MCP provider (local | composio) → external service → Result Normalizer → agent
```

Agents only ever see MCP-shaped tools (name, description, JSON-schema input), so nothing in an agent
depends on a provider's response format.

## Tool definition

Each discovered tool is stored in the `tools` table:

| Field | Meaning |
|---|---|
| `id` | `<provider>:<name>`, e.g. `local:http_fetch`, `composio:GITHUB_CREATE_AN_ISSUE` |
| `provider` | `local` or `composio` |
| `toolkit` | `github`, `aws`, `google`, `web`, `workspace`, `system` |
| `description`, `input_schema`, `output_schema` | taken from the MCP server |
| `capabilities` | tags used for matching |
| `risk_level` | `READ`, `WRITE` or `DESTRUCTIVE` |
| `requires_approval` | `DESTRUCTIVE` always; operators may also require it for others |
| `enabled` | disabled tools are never offered to agents |

`GET /api/tools` lists them, `POST /api/tools/refresh` re-discovers, `PATCH /api/tools/{id}` toggles
`enabled` / `requires_approval`. The UI shows all of this on the Integrations page.

## Risk classification

1. **MCP annotations win.** `readOnlyHint: true` → `READ`; `destructiveHint: true` → `DESTRUCTIVE`.
2. **Otherwise name heuristics**, deliberately conservative: `delete|remove|destroy|terminate|drop|purge|
   revoke|detach|reboot|restart|kill|…` → `DESTRUCTIVE`; `create|update|send|write|modify|commit|push|run|
   execute|trigger|apply|…` → `WRITE`; everything else `READ`.

`DESTRUCTIVE` tools always require human approval, and that cannot be relaxed through the API. Risk is
re-evaluated on every discovery, but an approval requirement an operator added is never removed
automatically.

## Execution guarantees

- **Argument validation** against the JSON schema (required fields, primitive types) before anything runs.
  Invalid calls are recorded as `BLOCKED` and the reason is handed back to the agent.
- **Approval gate.** For approval-required tools the task pauses in `WAITING_APPROVAL` with an `approvals`
  row until a human decides ([approvals in architecture](architecture.md)).
- **Write-ahead recording.** A `tool_executions` row (`RUNNING`) and a `TOOL_CALLED` event are committed
  *before* the provider is called, so a crash can never hide that a tool may have run.
- **At most one side effect.** For `WRITE`/`DESTRUCTIVE` tools, an identical call already completed in the
  same task is replayed from its recorded result. A call left `INTERRUPTED` is never re-run automatically —
  only after a human retries the task.
- **Recorded for every call:** task, step, agent, tool, redacted arguments + hash, risk level, status,
  normalized result, error, approval, start time and duration. `GET /api/tools/executions`.
- **Secrets never persist.** Arguments are redacted (keys named `token|secret|password|api_key|…`, Bearer
  headers, AWS key ids, Telegram tokens) before they reach the database or the logs.

## Normalized result

Every provider result becomes:

```json
{"status": "success|error|configuration_required|rejected",
 "summary": "one-line description",
 "data": {"…": "payload, truncated at 12k chars"},
 "evidence": [{"source": "url", "url": "https://…", "tool_id": "…", "retrieved_at": "…"}],
 "errors": [],
 "metadata": {"tool_id": "…", "provider": "…", "toolkit": "…", "risk_level": "READ"}}
```

Composio's `{"successful": …, "data": …, "error": …}` envelope is unwrapped, URLs are lifted into
`evidence` (which feeds evidence memory), and oversized payloads are truncated.

## Local MCP server

A real MCP server over Streamable HTTP (`http://mcp-local:8765/mcp`), so the whole agent → tool → result
path works with no external credentials. Source: `backend/app/integrations/mcp/local_server.py`.

| Tool | Risk | What |
|---|---|---|
| `get_current_time` | READ | Current date/time in an IANA timezone |
| `http_fetch` | READ | Fetch a public page/document, return title + readable text |
| `list_workspace_files` | READ | List files under a workspace path |
| `read_workspace_file` | READ | Read a UTF-8 text file |
| `search_workspace` | READ | Case-insensitive text search, returns matching lines |
| `git_repo_summary` | READ | Branch, recent commits, working-tree status |
| `write_workspace_note` | WRITE | Create/overwrite a note under `notes/` |
| `delete_workspace_file` | DESTRUCTIVE | Delete a file (needs approval) |
| `run_pytest` | DESTRUCTIVE | Run `pytest` for a workspace project (executes code, needs approval) |

Safety: all file paths are resolved and confined to `LOCAL_MCP_WORKSPACE` (the `workspace` volume);
`http_fetch` refuses private, loopback and link-local addresses (override with
`LOCAL_MCP_ALLOW_PRIVATE_FETCH=true`) and caps the download; setting `LOCAL_MCP_TOKEN` requires
`Authorization: Bearer …` on the MCP endpoint.

Put files where agents can read them:

```bash
docker compose cp ./my-repo mcp-local:/workspace/my-repo
docker compose exec mcp-local ls /workspace
```

## Composio MCP

Composio is the integration layer for GitHub, AWS and Google. Atlas talks to it as a plain MCP server, so
no Composio SDK is needed at runtime.

1. In the Composio dashboard, create an auth config and connect your accounts for the toolkits you want.
2. Create an MCP server/session for those toolkits and copy its Streamable HTTP URL and credentials.
3. In `.env`:

```bash
COMPOSIO_ENABLED=true
COMPOSIO_MCP_URL=<streamable http url>
COMPOSIO_API_KEY=<project api key>
# or, instead of the key, paste exactly what the SDK exports as session.mcp.headers:
# COMPOSIO_MCP_HEADERS={"x-api-key":"…","x-project-id":"…"}
COMPOSIO_USER_ID=<user id>     # appended as ?user_id=… when the URL has none
```

4. `docker compose up -d api worker`, then `POST /api/tools/refresh` (or the "Re-discover tools" button).
   `/api/integrations` should show `composio: OK` with a tool count.

Toolkit names map to Atlas tool ids (`composio:GITHUB_*`, `composio:AWS*`, `composio:GMAIL_*`,
`composio:GOOGLECALENDAR_*`, …), which is what the agents' glob patterns already expect.

Until it is configured, agents that need it say so instead of inventing data:
`CONFIGURATION REQUIRED: Composio MCP (aws toolkits) is not configured …`, and any attempted call is
recorded with status `CONFIGURATION_REQUIRED`.

> Verification note: the Composio path was exercised against a local MCP server and unit tests (URL and
> header construction, discovery, risk classification, `CONFIGURATION_REQUIRED` behaviour). It was not run
> against Composio's live service, because that needs an account.

## Adding your own tools

- **Local tool:** add a `@mcp.tool(...)` function in `local_server.py` with an `annotations=` risk hint and
  a `meta={"toolkit": …, "capabilities": [...]}` tag, then restart `mcp-local` and refresh. Type hints
  become the JSON schema.
- **Another MCP server:** add an `MCPToolProvider("myprovider", url, headers=…)` in
  `backend/app/tools/registry.py::build_providers` and grant `myprovider:*` to an agent.

## Failure handling

An unreachable provider is reported as `UNREACHABLE` in `/api/integrations`, and the worker retries
discovery every 20 seconds while any configured provider is degraded (otherwise every
`TOOL_REFRESH_SECONDS`). Individual call failures are normalized to `status: "error"`, handed back to the
agent, and surface as warnings in validation rather than crashing the task.
