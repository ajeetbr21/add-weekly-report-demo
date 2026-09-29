# Learning

Atlas improves by turning your feedback into retrievable procedures. The pipeline is deliberately
conservative: a lesson has to be extracted, validated and (normally) approved before it can influence
anything, and it can only ever change memory — never code, instructions or configuration.

```
feedback → parser → learning candidate → validation → approval → lesson (+ skill)
        → PROCEDURAL / SEMANTIC memory → retrieved by future similar tasks → planning guidance
```

## 1. Feedback

Web UI (task page): 👍 Worked, 👎 Incorrect, or free text.
Telegram: the inline buttons or `/feedback TASK-2026-000001 <text>`.
API: `POST /api/feedback` with `task_id`, `content` and an optional `rating`.

Feedback is stored in `feedback`, linked to the task, and a `FEEDBACK_RECEIVED` journal event is written.

## 2. Parsing

The parser extracts only guidance that should change **future behaviour**. Praise and complaints produce no
lessons.

- **LLM parser** (the `fast` tier) when a model is available.
- **Rule-based parser** always as the baseline and fallback: it detects the rating from sentiment words and
  keeps sentences that are imperative ("before…", "always", "never", "check…", "use…") or contain
  forward-looking phrases ("next time", "going forward", "from now on"). Leading filler is stripped and the
  sentence is normalized into a standalone lesson.

Category and confidence:

| Category | Example |
|---|---|
| `PROCEDURE` | "Before restarting anything, check CloudWatch status and recent events." |
| `ANTI_PATTERN` | "Do not use the force-restart procedure." |
| `PREFERENCE` | "Prefer shorter summaries." |
| `FACT` | "Our production database is RDS PostgreSQL 16 in eu-west-1." |

Confidence starts at 0.6, rises for imperative phrasing (+0.12) and recognized domain words (+0.08), and
drops for hedging ("maybe", "might", −0.2).

## 3. Candidate and validation

Each lesson becomes a `learning_candidates` row: original feedback, normalized lesson, category, domain
tags (the domain words plus the agents involved and their memory-policy tags), confidence, status and
validation notes.

Validation then:

- **Detects duplicates.** If an equivalent memory already exists (identical content, or ≥ 0.9 similarity),
  the candidate is `REJECTED` with a note and the *existing* memory is reinforced instead.
- **Flags protected areas.** A lesson mentioning source code, security, approvals, credentials, system
  instructions or the schema always requires human approval, whatever its confidence.
- **Auto-approves** only when it is not flagged, confidence ≥ `LEARNING_AUTO_APPROVE_THRESHOLD` (0.85) and
  the category is `PROCEDURE`, `PREFERENCE` or `ANTI_PATTERN`. Set the threshold to `1.1` to require human
  approval for everything.

Statuses: `CANDIDATE` → `APPROVED` | `REJECTED` | `DEPRECATED`.

## 4. Approval

Learning page in the UI, or `POST /api/learning/candidates/{id}/decision` with `{"approve": true}`. You can
edit the wording before approving (`edited_lesson`), which is the normal way to tighten a lesson.

Approval creates:

1. A memory — `PROCEDURAL` for procedures and anti-patterns, `SEMANTIC` for facts — with confidence ≥ 0.8
   and importance 0.85, tagged `lesson` plus its domain tags.
2. A `lessons` row that tracks usage and outcomes.
3. For procedures, a **skill**: a per-domain playbook (`aws-playbook`, `coding-playbook`, …) that collects
   related procedures and is versioned as it grows.

Every decision is written to `audit_logs` with the actor.

## 5. Reuse

A later task retrieves the lesson through normal memory search ([memory.md](memory.md#retrieval)), where
`PROCEDURAL` records are boosted. From there:

- The **planner** receives them as explicit guidance ("Learned procedure: …"), which is also stored in the
  plan.
- The **agent** gets them in its context under "Relevant memory", and `PROCEDURAL` records are always
  allowed through, regardless of the agent's memory policy.
- The task result lists them as `applied_lessons`, the UI shows "Lessons applied from memory", and each
  lesson's `usage_count` is incremented.

## 6. Outcome tracking and forgetting

When you rate a task that applied lessons, those lessons get `success_count` or `failure_count`. A lesson
used at least 3 times with a success rate below 34% is automatically `DEPRECATED`, and its memory is
deprecated too, so it stops being retrieved. You can deprecate manually at any time on the Learning page or
via `POST /api/learning/lessons/{id}/deprecate`.

## Worked example (verified end to end)

1. *"Analyze today's AWS alarms…"* → AWS/DevOps agent answers; the task is journaled and consolidated.
2. Feedback: *"Before restarting anything, check CloudWatch status and recent events."*
   → candidate: category `PROCEDURE`, confidence 0.80, tags `aws, aws_devops, cloudwatch, devops`.
3. Approve it → `PROCEDURAL` memory + `lessons` row + `aws-playbook` skill v1.
4. New task: *"The EC2 app server is down after the AWS alarm fired. Should we restart it?"*
   → memory search returns the lesson first; the plan carries it as guidance; the result shows
   "Lessons applied from memory: Before restarting anything, check CloudWatch status and recent events."

Screenshots: [learning candidate](screenshots/04a-learning-candidate.png),
[approved lesson](screenshots/04b-learning-lesson-approved.png),
[lesson applied](screenshots/05-follow-up-applies-lesson.png).

## Safety: no unsafe self-modification

Learning writes to exactly four places: memories, lessons, skills, and the planning/retrieval context. It
**cannot** modify source code, agent instructions, security or approval settings, tool risk levels,
credentials or the database schema. There is no code path from feedback to any of those: editing an agent
is a separate, audited operator action (`PATCH /api/agents/{id}`), and code changes go through a normal
branch and pull request. Lessons that merely *mention* a protected area are still only stored as guidance,
and always require human approval first.

## Manual skills

You can define a procedure directly, without waiting for feedback:

```bash
curl -s localhost:8000/api/learning/skills -H "Authorization: Bearer $ATLAS_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name": "rds-snapshot-restore",
    "description": "Restore an RDS instance from a snapshot",
    "domain_tags": ["aws", "rds"],
    "steps": ["Find the latest snapshot", "Restore to a new instance", "Swap the endpoints"]}'
```

It is stored as `PROCEDURAL` memory as well, so it is retrievable immediately.

`GET /api/learning/summary` gives counts per status, approved lessons, skills and feedback ratings.
