# Memory

Atlas separates **session memory** (temporary working state) from **long-term memory** (PostgreSQL +
pgvector). Nothing moves from the first to the second automatically — only the consolidation pipeline may
create long-term memories.

## Session memory (temporary)

| Scope | Where | Contents |
|---|---|---|
| Current task | `tasks.session` (JSONB) | normalized request, retrieved memory context, plan guidance, per-step results, applied lesson ids |
| Current conversation (`session_key`, e.g. `telegram:<chat>`, `web:<user>`) | Letta memory blocks `conversation`, `active_task`, `scratchpad` — mirrored in the `sessions` table | last 8 exchanges, the active task and its plan, scratch values |

Session memory survives restarts because it is in the database, but it is not knowledge: it is never
searched by future tasks. If Letta is disabled or unreachable, the local `sessions` table is used and the
task result records `session_runtime: "local"` with a warning.

## Long-term memory

| Type | Holds | Typical source |
|---|---|---|
| `CORE` | Always-relevant facts about you or the setup | added manually |
| `SEMANTIC` | Durable facts and knowledge | agent `facts`, manual entry |
| `EPISODIC` | What was attempted, which tools were used, what succeeded or failed, the outcome | task consolidation |
| `PROCEDURAL` | Reusable procedures and anti-patterns ("check CloudWatch before restarting") | approved lessons, skills |
| `EVIDENCE` | Provenance: URL, title, tool, task, timestamp | tool results |

Each record has `content`, `title`, `tags`, `source`, `task_id`, `project_id`, `agent_id`, `confidence`,
`importance`, `status` (`ACTIVE|SUPERSEDED|DEPRECATED|ARCHIVED`), `usage_count` and `last_accessed_at`.

## Embeddings

Every memory is embedded and stored in `memory_embeddings` with the model that produced the vector, so
switching models later is safe — searches only compare vectors from the same model.

- **Gateway embeddings** when `EMBEDDING_MODEL` is set: OmniRoute `/v1/embeddings`.
- **`local-hash-v1` always**, as well: a deterministic signed feature-hashing embedding over word
  unigrams, bigrams and character trigrams (stemmed, stop-worded). It is lexical — it matches wording, not
  meaning — but it needs no model and no network, so retrieval keeps working when the gateway is down.

If the gateway embedding fails or returns the wrong width, the write still succeeds with the local vector.

## Retrieval

```
task → embed the normalized request → pgvector cosine search (HNSW index)
     → filter → re-rank → top-k → injected into the agent's context
```

**Filters:** `status = ACTIVE`; memory types allowed by the agent's `memory_policy` (`PROCEDURAL` is always
allowed); project scope — the task's project plus global memories that belong to no project; agent scope.

**Ranking** combines cosine similarity with metadata rather than using distance alone:

| Signal | Weight |
|---|---|
| similarity | 0.62 |
| importance | 0.14 |
| confidence | 0.10 |
| recency (30-day half-life) | 0.09 |
| usage count | 0.05 |

Plus small boosts: `PROCEDURAL` +0.06, `CORE` +0.05, `SEMANTIC` +0.02, same project +0.03. Results below
the similarity threshold are dropped (`MEMORY_MIN_SCORE` 0.3 for gateway embeddings,
`MEMORY_MIN_SCORE_HASH` 0.1 for the lexical ones, whose natural range is lower). Retrieved memories get
`usage_count += 1` and a fresh `last_accessed_at`.

Only the top-k (`MEMORY_TOP_K`, default 6) are injected, each truncated — the memory database is never
dumped into a prompt. The `MEMORY_RETRIEVED` journal event records exactly what was used, with scores.

Try it yourself: Memory page in the UI, or

```bash
curl -s localhost:8000/api/memory/search -H "Authorization: Bearer $ATLAS_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"query":"restart EC2 after an alarm","limit":5}'
```

## Consolidation

```
finished task → candidate memories → evaluation → classification → store | update | reject
```

Candidates per task:

- **One episodic record**: request, agents, tools used, failed/blocked tools, outcome. Importance is higher
  when the task failed or tools failed.
- **Evidence records** for each external URL actually retrieved, with tool, task and timestamp.
- **Semantic facts** proposed by the agent in its `facts` field (LLM mode only).

Rejection rules (this is what prevents memory pollution):

| Rejected when | Applies to |
|---|---|
| trivial request ("hi", "thanks", under 8 characters) | episodic |
| task not finished | episodic |
| shorter than 15 or longer than 600 characters | semantic |
| hedged wording ("maybe", "might", "probably", "not sure") | semantic |
| confidence below 0.6, or the task result failed validation | semantic |
| the same content already exists in this scope | all |

A duplicate is not stored twice: the existing record is reinforced (confidence nudged up, importance raised
to the maximum of the two, tags merged). Every decision is summarized in the `MEMORY_CREATED` journal event
and counted in the task result as `{"stored": n, "reinforced": n, "rejected": n}`.

## Managing memory

| Action | How |
|---|---|
| Add knowledge | Memory page, or `POST /api/memory` (`type`, `content`, `title`, `tags`, `project`) |
| Search | Memory page, or `POST /api/memory/search` |
| Browse / filter | `GET /api/memory?type=PROCEDURAL&q=cloudwatch` |
| Archive or deprecate | "archive" in the UI, or `PATCH /api/memory/{id}` with `status` |
| Counts per type | `GET /api/memory/stats` |

Deleting is deliberately not exposed: archiving keeps the audit trail. `ARCHIVED` and `DEPRECATED` records
are excluded from retrieval.

## Project scope

Tasks belong to a project (auto-detected from project keywords, or set explicitly). Retrieval then sees
that project's memories plus global ones, so an "AWS Operations" task will not be answered with facts that
belong to another workspace. Create projects on the Settings page or via `POST /api/projects` with the
keywords that should match.
