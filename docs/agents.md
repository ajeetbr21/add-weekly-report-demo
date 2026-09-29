# Agents

An agent is a row in the `agents` table: identity, capabilities, routing keywords, allowed tools,
instructions, a model tier and a memory policy. The orchestrator discovers agents through the registry, so
adding one never means touching orchestrator code.

## Built-in agents

| Agent | `id` | Model tier | Capabilities |
|---|---|---|---|
| Research Agent | `research` | `reasoning` | research, documentation analysis, information extraction, structured summaries, evidence collection |
| Coding Agent | `coding` | `coding` | repository analysis, code generation, code modification, debugging, test execution, code review, GitHub workflows |
| AWS / DevOps Agent | `aws_devops` | `reasoning` | AWS investigation, EC2, RDS, S3, EKS, CloudWatch, infrastructure troubleshooting, cost analysis, operational workflows |
| General Automation Agent | `general` | `default` | generic automation, multi-step tasks, tool execution, task coordination |

Tools are granted as glob patterns over tool ids, so an agent automatically picks up new matching tools:

| Agent | Tool patterns |
|---|---|
| `research` | `local:http_fetch`, `local:get_current_time`, `local:read_workspace_file`, `local:search_workspace`, `local:list_workspace_files`, `composio:*SEARCH*`, `composio:GOOGLEDOCS_*`, `composio:GOOGLEDRIVE_*` |
| `coding` | `local:list_workspace_files`, `local:read_workspace_file`, `local:search_workspace`, `local:git_repo_summary`, `local:write_workspace_note`, `local:run_pytest`, `local:delete_workspace_file`, `composio:GITHUB_*` |
| `aws_devops` | `composio:AWS*`, `composio:COMPOSIO_*`, `local:get_current_time`, `local:http_fetch` |
| `general` | `local:*`, `composio:*` |

`general` is the fallback (`FALLBACK_AGENT_ID`). Source: `backend/app/agents/definitions.py`.

## Routing

`AgentRegistry.route()` scores every **active** agent against the request:

- Request words are matched against the agent's `keywords` (strong words count 1.0, short ones 0.6) and
  against words from its `capabilities` (0.5 each).
- URLs, file paths and file names are stripped first, so *"delete `notes/restart-demo.txt`"* does not route
  to the AWS agent because of the word "restart".
- `general` is deliberately weak: its score is scaled to `score * 0.6 + 0.3`, so it wins only when no
  specialist matches.
- On a tie, a specialist beats the generalist. A specialist needs a score of at least 1.0 to be chosen.

The planner then decides simple vs complex ([architecture.md](architecture.md#planner-apporchestratorplannerpy)).
The scores and the matched words are stored in the plan and shown in the UI ("Plan & steps" → rationale).

## Execution loop

`backend/app/agents/runner.py` runs the same loop for every agent:

1. Build the context: instructions + objective + original request + results of the steps this one depends
   on + recent conversation + retrieved memories + planning guidance.
2. Call the agent's model tier through OmniRoute with the agent's tools as OpenAI function definitions.
3. Execute returned tool calls through the `ToolExecutor` (records, approval gate, normalization), append
   the normalized results and loop.
4. Stop when the model answers instead of calling a tool, or after `AGENT_MAX_ITERATIONS` (6) — the last
   iteration asks for the final answer with tools disabled.

Every agent must answer with one JSON object:

```json
{"summary": "...", "findings": ["..."], "actions_taken": ["..."],
 "recommendations": ["..."], "facts": ["durable facts worth remembering"], "confidence": 0.8}
```

Non-JSON answers are still accepted and used as the summary. `facts` is the only path by which an agent can
propose long-term semantic memory, and consolidation still validates it.

**Checkpoints.** After every tool round the message history, iteration count and tool log are committed to
`task_steps.checkpoint`. A step paused for approval or interrupted by a crash resumes from there instead of
starting over. Each attempt is recorded as its own `agent_runs` row (model, provider, iterations, tokens),
and interrupted attempts are marked `INTERRUPTED`.

**Offline mode.** Without a usable LLM the runner switches to the deterministic engine in
`backend/app/agents/heuristics.py`: it ranks the agent's tools by word overlap with the request, and only
calls a tool when every required argument can be derived from the request itself (a URL, an AWS region, a
quoted string, a path, or a schema default). Mutating tools run only if the user explicitly asked for that
exact action. The report is then assembled from real tool results and retrieved memory, and labelled
`Offline mode (no LLM available: …)` with `offline: true`.

## Operating rules in every prompt

All agents share `COMMON_RULES`: stay on the objective; use tools instead of inventing output; say
`CONFIGURATION REQUIRED: …` when an integration is missing; prefer read-only steps and expect destructive
actions to pause for approval; apply the retrieved lessons. The coding agent additionally must route changes
to Atlas itself through a normal branch + pull request.

## Editing and adding agents

**Editing at runtime** (audited operator action, `PATCH /api/agents/{id}`, or the Agents page in the UI):
`description`, `instructions`, `capabilities`, `keywords`, `tools`, `model_preference`
(`default|fast|reasoning|coding`), `status` (`ACTIVE|DISABLED`). Disabled agents are not routed to and
cannot appear in a plan. The learning engine never writes here — only memory, lessons and skills.

**Adding a new agent:** append an `AgentSpec` to `BUILTIN_AGENTS` in `backend/app/agents/definitions.py` and
restart. Startup inserts missing agents with `INSERT … ON CONFLICT DO NOTHING`, so existing (possibly edited)
rows are never overwritten. Nothing else changes: routing, planning and the tool loop are generic.

```python
SECURITY = AgentSpec(
    id="security",
    name="Security Agent",
    description="Reviews dependencies and findings, triages CVEs.",
    capabilities=["vulnerability analysis", "dependency review", "cve triage"],
    keywords=["cve", "vulnerability", "security", "dependency", "audit", "patch"],
    tools=["local:http_fetch", "local:read_workspace_file", "composio:GITHUB_*"],
    instructions=f"You are the Security Agent of Atlas.\n…\n{COMMON_RULES}",
    model_preference="reasoning",
    memory_policy={"retrieve_types": ["SEMANTIC", "PROCEDURAL", "EPISODIC"], "top_k": 6, "tags": ["security"]},
)
```

## Multi-agent tasks

A request such as *"Investigate this AWS EC2 problem, research the documentation, and prepare a report"*
produces a plan like:

```
step 0  aws_devops  investigate the infrastructure side          depends_on: []
step 1  research    research documentation and best practices    depends_on: [0]
→ synthesis of the validated results + a stored report
```

Steps run sequentially. Each step sees the results of the steps it depends on. Agents cannot call each
other: only the orchestrator starts agents, dependencies may only point backwards, and plans are capped at
5 steps, so uncontrolled agent-to-agent loops are structurally impossible.

## Memory policy per agent

`memory_policy` controls retrieval for that agent: `retrieve_types` (which memory types it may see),
`top_k`, and `tags` used when its episodes are stored. `PROCEDURAL` is always added, so learned lessons
always reach the agent. Details: [memory.md](memory.md).
