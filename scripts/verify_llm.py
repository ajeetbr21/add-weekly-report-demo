"""Prove the real-LLM path end to end, with no API key and no signup.

It starts nothing by itself except a local MCP server: you provide a PostgreSQL database and an
OpenAI-compatible model endpoint. The easiest zero-credential endpoint is Ollama:

    docker run -d --name ollama -p 11434:11434 -v ollama-models:/root/.ollama ollama/ollama
    docker exec ollama ollama pull qwen2.5:1.5b          # ~1 GB, supports tool calling, CPU is fine
    scripts/dev-db.sh start                              # PostgreSQL + pgvector on localhost:5432

    cd backend && DATABASE_URL=postgresql+asyncpg://atlas:atlas@localhost:5432/atlas \
      OMNIROUTE_ENABLED=true OMNIROUTE_BASE_URL=http://localhost:11434/v1 OMNIROUTE_API_KEY=local \
      MODEL_DEFAULT=qwen2.5:1.5b MODEL_REASONING=qwen2.5:1.5b LLM_OFFLINE_FALLBACK=false \
      .venv/bin/python ../scripts/verify_llm.py

The same command works against any OpenAI-compatible endpoint (OmniRoute, Groq, OpenRouter, …): only
OMNIROUTE_BASE_URL, OMNIROUTE_API_KEY and the MODEL_* names change.

It prints the plan, the model calls, the real tool call and the answer, and exits non-zero unless a real
model produced the result (`offline == False`).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_local_mcp() -> None:
    """Run Atlas' own MCP server in-process so the agent has a real tool to call."""
    import uvicorn

    from app.integrations.mcp import local_server

    port = int(os.environ["LOCAL_MCP_URL"].rsplit(":", 1)[1].split("/")[0])
    local_server.WORKSPACE = Path(os.environ["LOCAL_MCP_WORKSPACE"])
    local_server.WORKSPACE.mkdir(parents=True, exist_ok=True)
    server = uvicorn.Server(uvicorn.Config(local_server.mcp.streamable_http_app(), host="127.0.0.1",
                                           port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("local MCP server did not start")


REQUEST = os.environ.get("VERIFY_REQUEST",
                         "What is the current time in UTC right now? Use a tool to find out, then state it.")


async def main() -> int:
    from sqlalchemy import select

    from app.agents.registry import sync_builtin_agents
    from app.database.session import session_scope
    from app.integrations.omniroute.client import get_model_client
    from app.models import AgentRun, Task, TaskEvent, ToolExecution
    from app.schemas.api import TaskCreate
    from app.tasks.service import create_task, ensure_default_project
    from app.tools.registry import get_tool_registry
    from app.worker import Worker

    model = get_model_client()
    if not model.configured:
        print("FAIL: set OMNIROUTE_ENABLED=true and OMNIROUTE_BASE_URL (see this file's docstring)")
        return 2
    health = await model.health()
    print(f"gateway: {health.get('status')} | models: {(health.get('models') or [])[:5]}")
    print(f"routing: {health.get('routing')}")
    if health.get("status") != "OK":
        print(f"FAIL: gateway not usable: {health.get('detail')}")
        return 2

    async with session_scope() as s:
        await sync_builtin_agents(s)
        await ensure_default_project(s)
    async with session_scope() as s:
        providers = await get_tool_registry().refresh(s)
    print(f"tools:   {({k: v.get('status') for k, v in providers.items()})} "
          f"| local: {providers.get('local', {}).get('tools')}")

    async with session_scope() as s:
        task, _ = await create_task(s, TaskCreate(request=REQUEST, source="api", session_key="api:verify-llm"))
        task_id = task.id
    print(f"task:    {task_id} — running through the real orchestrator …")
    started = time.time()
    await Worker().run_once()
    took = time.time() - started

    async with session_scope() as s:
        t = await s.get(Task, task_id)
        runs = (await s.execute(select(AgentRun).where(AgentRun.task_id == task_id))).scalars().all()
        tools = (await s.execute(select(ToolExecution).where(ToolExecution.task_id == task_id))).scalars().all()
        events = (await s.execute(select(TaskEvent).where(TaskEvent.task_id == task_id)
                                  .order_by(TaskEvent.id))).scalars().all()
    assert t is not None
    res = t.result or {}
    plan = t.plan or {}
    print(f"\n===== result ({took:.0f}s) =====")
    print(f"status   : {t.status} | agent: {t.selected_agent}")
    print(f"planner  : {plan.get('planner')} | model: {plan.get('planner_meta', {}).get('model')}")
    print(f"offline  : {res.get('offline')}   <- False means a real model produced this")
    print(f"summary  : {(res.get('summary') or '')[:500]}")
    for r in runs:
        print(f"agent run: {r.agent_id} {r.status} provider={r.model_provider} model={r.model} "
              f"iterations={r.iterations} tokens={r.prompt_tokens}+{r.completion_tokens}")
    print(f"tools    : {[(x.tool_id, x.status, x.duration_ms) for x in tools]}")
    for e in events:
        if e.event_type == "MODEL_CALL":
            m = e.extra
            print(f"model call: {m.get('provider')}/{m.get('model')} offline={m.get('offline')} "
                  f"usage={m.get('usage')} tool_calls={m.get('tool_calls')} {m.get('duration_ms')}ms")
    print(f"journal  : {' > '.join(e.event_type for e in events)}")
    print(f"memory   : {res.get('memory')}")

    ok = (t.status == "COMPLETED" and res.get("offline") is False
          and any(r.model_provider == "omniroute" for r in runs))
    used_tool = any(x.status == "SUCCESS" for x in tools)
    print(f"\nreal LLM answered : {'yes' if ok else 'NO'}")
    print(f"real tool executed: {'yes' if used_tool else 'no (the model answered without calling one)'}")
    print("VERIFY_LLM:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("LOCAL_MCP_WORKSPACE", tempfile.mkdtemp(prefix="atlas-verify-"))
    os.environ.setdefault("LOCAL_MCP_URL", f"http://127.0.0.1:{_free_port()}/mcp")
    os.environ.setdefault("LETTA_ENABLED", "false")
    os.environ.setdefault("LOG_JSON", "false")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    _start_local_mcp()
    sys.exit(asyncio.run(main()))
