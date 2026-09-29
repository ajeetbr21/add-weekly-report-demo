# Telegram

The bot is a thin interface: it creates tasks and shows results through the local API. It holds no business
logic and has no database access — the same pipeline handles a Telegram request as a Web UI request.

```
you → Telegram → bot (long polling) → POST /api/tasks → worker → orchestrator → agent → tools
                                                                              ↓
                       you ← bot ← poll GET /api/tasks/{id} ← result, approvals, feedback buttons
```

## Setup

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Get your numeric chat id: message the bot once, then
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"` and read `message.chat.id`.
3. In `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_ALLOWED_CHAT_IDS=123456789      # comma-separated; anyone else is refused
```

4. `docker compose up -d telegram && docker compose logs -f telegram` — you should see
   `telegram bot connected as @yourbot`.

The bot uses **long polling**, so no public URL, tunnel or webhook is needed. It reaches the API at
`ATLAS_API_URL` (`http://api:8000` under Compose) and sends `ATLAS_API_TOKEN` with every request.

Without a token the container logs
`Telegram CONFIGURATION REQUIRED: set TELEGRAM_BOT_TOKEN …` and idles, so the rest of the stack stays
healthy. `/api/integrations` reports `telegram: CONFIGURATION_REQUIRED`.

> Verification note: the handlers, command parsing, approval and feedback callbacks, and the API client
> against the real FastAPI app are covered by automated tests. The bot was not run against Telegram's live
> service, because that needs a BotFather token.

## Usage

Any normal message becomes a task:

> **You:** Check today's AWS alarms
> **Atlas:** Task TASK-2026-000042 created. I'll report back when it's done.
> **Atlas:** TASK-2026-000042 — COMPLETED
> 2 alarms need attention …
> • CPUUtilization high on web-1
> → Check CloudWatch events before restarting
> [👍 Worked] [👎 Incorrect]

| Command | What |
|---|---|
| `/start`, `/help` | usage |
| `/status` | system health, active/completed/failed counts, pending approvals, approved lessons |
| `/tasks` | your 8 most recent tasks with status |
| `/agents` | available agents and their status |
| `/approvals` | pending approvals, each with APPROVE / REJECT buttons |
| `/feedback TASK-2026-000042 <text>` | feedback that can become a learned lesson |

## Approvals from your phone

When a task needs a destructive action, the bot pushes it while you are waiting:

> Approval required for TASK-2026-000042
> Action: `delete_workspace_file(path="notes/old.txt")`
> Risk: DESTRUCTIVE
> [✅ APPROVE] [❌ REJECT]

Tapping a button calls the same `POST /api/approvals/{id}/decision` endpoint the Web UI uses. The decision
is recorded in the journal and in `audit_logs` with `channel: telegram` and your username, and the task
resumes from its checkpoint. Arguments shown in Telegram are redacted like everywhere else.

## Feedback and learning

👍 stores "This solution worked." (POSITIVE), 👎 stores "That was incorrect." (NEGATIVE) and then asks you
to say what should change, which is where real lessons come from:

```
/feedback TASK-2026-000042 Before restarting anything, check CloudWatch status and recent events.
```

The bot replies with how many learning candidates were created. Review them on the Learning page
([learning.md](learning.md)).

## Behaviour and limits

- The bot polls a task for up to 15 minutes (`TELEGRAM_RESULT_POLL_SECONDS`, default 3 s, between checks).
  Longer tasks keep running — use `/tasks` or the Web UI to check later.
- Each chat is its own session (`session_key = telegram:<chat_id>`), so the last exchanges are available as
  conversation context ([memory.md](memory.md#session-memory-temporary)).
- Replies are capped at Telegram's 4096-character limit; full results live in the Web UI.
- Leaving `TELEGRAM_ALLOWED_CHAT_IDS` empty lets **any** chat use the bot. The container warns about it at
  startup. Always set it.
- Network hiccups are retried with a back-off; one failing update never stops the bot.
