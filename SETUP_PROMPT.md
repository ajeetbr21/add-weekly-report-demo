# Setup prompt for an AI assistant

Paste the block below into an AI coding assistant (GitHub Copilot Chat, Cursor, Claude, Gemini Code Assist,
Windsurf, Kiro …) with a terminal, inside the folder where you want Atlas to live. It installs and starts
everything, verifies it, and reports honestly.

**Shell note (Windows):** use a **WSL2 / Ubuntu** terminal or **Git Bash**, not PowerShell — the setup
script is bash. Docker Desktop must be running.

If you would rather not use an AI at all, the same thing by hand:

```bash
git clone https://github.com/ajeetbr21/atlas.git
cd atlas
scripts/bootstrap.sh --demo
```

---

## Copy from here

````text
You are setting up "Atlas", a local-first multi-agent AI platform, on my machine. It runs entirely in
Docker Compose. Work through the steps in order, run the commands yourself, and fix problems you can fix.

RULES
- Never invent, guess or generate API keys, tokens or credentials. If a step needs a credential I have not
  given you, stop and ask me for it, or skip that optional step and tell me it is skipped.
- Do not modify the application source code. This is an installation task, not a development task. The
  only file you may edit is `.env`.
- Do not claim something works unless you ran it and saw it succeed. If a command fails, show me the real
  error and what you tried.
- Everything binds to 127.0.0.1. Do not expose ports publicly or change that.

STEP 1 — Prerequisites
Check and report versions: `docker --version`, `docker compose version`, `docker info` (the daemon must
respond), and free disk space (~10 GB needed; the model-gateway image alone is ~4 GB).
If Docker is missing or not running, tell me exactly what to install or start, then stop and wait.
Confirm Docker has at least 8 GB of memory (Docker Desktop → Settings → Resources). If it has less, warn me.

STEP 2 — Get the code
If a folder named `atlas` is not already here, run:
    git clone https://github.com/ajeetbr21/atlas.git
Then `cd atlas`. Run every later command from that folder. If it already exists, run `git pull` instead.

STEP 3 — Read before acting
Read `README.md` and `docs/setup.md`. Then tell me in three or four sentences what this system does and
which services will start. This makes sure you are working from the real project, not assumptions.

STEP 4 — Install and start
Run:
    chmod +x scripts/*.sh
    scripts/bootstrap.sh
This generates `.env` with fresh secrets (it never overwrites values that already exist), builds the
images, starts nine services, applies the database migrations and waits for readiness. The first run
downloads several GB and can take 5–15 minutes — let it finish, do not interrupt it.
If it fails, read the error, consult `docs/troubleshooting.md`, apply the documented fix and retry once.
If it still fails, show me the output of `docker compose ps` and `docker compose logs --tail=40`.

STEP 5 — Verify it actually works
Run each of these and show me the real output:
  a) `curl -s localhost:8000/ready`
     Expect: {"status":"ready","database":{"status":"OK","migration":"0001","pgvector":"..."}}
  b) `docker compose ps` — all nine services up. `migrate` showing "Exited (0)" is correct: it applies the
     migrations and exits.
  c) `scripts/demo.sh` — the end-to-end demo. Expect "RESULT: ALL CHECKS PASSED". It creates a task,
     shows the journal, tool calls, memory, feedback → a learned lesson, and a destructive action that
     pauses for approval.
  d) Open http://localhost:3000 and confirm the dashboard loads and the sidebar says "backend ready".
Then report which checks passed and which did not. Do not paper over a failure.

STEP 6 — Explain the current state
Run `curl -s localhost:8000/api/integrations -H "Authorization: Bearer $(grep '^ATLAS_API_TOKEN=' .env | cut -d= -f2)"`
and explain it to me. Expect `database`, `letta` and `mcp_local` to be OK, and `omniroute`, `composio` and
`telegram` to be CONFIGURATION_REQUIRED because they need my credentials. That is normal, not a bug:
without a model provider the agents run in a clearly labelled offline mode and never fake results.

STEP 7 — Offer the optional integrations, do not do them silently
Ask me which of these I want now, and only then do it:
  (a) Real LLM reasoning — two choices:
      - Fully local, no signup: install Ollama, `ollama pull qwen2.5:1.5b`, then in `.env` set
        OMNIROUTE_ENABLED=true, OMNIROUTE_BASE_URL=http://host.docker.internal:11434/v1,
        OMNIROUTE_API_KEY=local, and MODEL_DEFAULT / MODEL_FAST / MODEL_REASONING / MODEL_CODING to the
        model name. (On Linux, if host.docker.internal does not resolve, use the host's IP or run Ollama
        as a container on the compose network.)
      - A provider key I give you (OmniRoute at http://localhost:20128, or any OpenAI-compatible endpoint
        such as Groq or OpenRouter): put the base URL, key and model names in `.env`.
      After either, run `docker compose up -d api worker`, then check `/api/integrations` shows
      omniroute OK, and finally run `scripts/verify_llm.py` (its docstring has the exact command) and show
      me the result. It must print offline=False and VERIFY_LLM: PASS.
  (b) Telegram — only if I give you a BotFather token. Set TELEGRAM_BOT_TOKEN and
      TELEGRAM_ALLOWED_CHAT_IDS (my numeric chat id), then `docker compose up -d telegram` and show me the
      log line confirming the bot connected. See docs/telegram.md.
  (c) Composio (GitHub / AWS / Google tools) — only if I give you a Composio MCP URL and key. See
      docs/tools.md. Afterwards call POST /api/tools/refresh and show me the new tool count.

STEP 8 — Final report
Give me a short summary:
  - what is running and on which URLs
  - my API token (from .env) so I can call the API directly
  - which checks passed, and anything that failed with the real reason
  - what is still CONFIGURATION_REQUIRED and the exact next step for each
  - the everyday commands: start, stop, logs, rebuild, reset
Also point me at `AI.md`, which documents how the platform was built, every design decision and every bug
that was found and fixed.
````

## Up to here

---

## What you end up with

| URL | What |
|---|---|
| http://localhost:3000 | Web dashboard: tasks, agents, memory, learning, approvals, reports, schedules |
| http://localhost:8000/docs | API with interactive OpenAPI docs |
| http://localhost:20128 | OmniRoute model gateway dashboard |

Nine local containers: `postgres` (+pgvector), `migrate`, `api`, `worker`, `mcp-local`, `telegram`,
`frontend`, `omniroute`, `letta`. Nothing leaves your machine until you connect a model provider yourself.

## If your AI gets stuck

Point it at [docs/troubleshooting.md](docs/troubleshooting.md) — it lists the real failure modes with
causes and fixes (ports in use, low Docker memory, `migrate` exiting, offline mode, a `502` from the UI
proxy, and more). Or just run the commands yourself; `scripts/bootstrap.sh` is the whole install.
