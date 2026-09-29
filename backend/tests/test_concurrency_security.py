"""Cancellation races, lock-safe transitions, MCP auth, Composio header handling."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.database.session import session_scope
from app.integrations.mcp.client import MCPToolProvider
from app.integrations.omniroute.client import ModelClient
from app.models import Approval
from app.orchestrator.orchestrator import Orchestrator
from app.tools.base import ToolProviderUnavailable
from app.tools.registry import build_providers, get_tool_registry
from app.worker import Worker
from tests.helpers import final, tool_call


async def _events(client, task_id):
    return [e["event_type"] for e in (await client.get(f"/api/tasks/{task_id}/events")).json()]


async def test_cancel_during_agent_loop_is_not_overwritten(client, settings):
    """The user cancels while the agent loop is still running (between two model calls).
    The worker must not overwrite CANCELLED with COMPLETED."""
    t = (await client.post("/api/tasks", json={"request": "What time is it now in UTC?"})).json()["task"]
    state = {"cancel_status": None}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("response_format"):  # planner
            msg = {"role": "assistant", "content": '{"complexity":"simple","steps":[{"agent_id":"general",'
                                                   '"objective":"tell the time","depends_on":[]}]}'}
        elif not any(m.get("role") == "tool" for m in body["messages"]):
            msg = tool_call("local__get_current_time", {"timezone": "UTC"})
        else:
            # cancel arrives while the agent's transaction is still open
            r = await client.post(f"/api/tasks/{t['id']}/cancel")
            state["cancel_status"] = r.json()["status"]
            msg = final("It is noon.")
        return httpx.Response(200, json={"model": body.get("model"), "choices": [{"message": msg}], "usage": {}})

    s2 = settings.model_copy(update={"omniroute_enabled": True, "omniroute_base_url": "http://gw.test"})
    model = ModelClient(settings=s2, transport=httpx.MockTransport(handler))
    await asyncio.wait_for(Worker(orchestrator=Orchestrator(model=model)).run_once(), timeout=30)
    assert state["cancel_status"] == "CANCELLED"  # the cancel was not blocked by the running agent
    task = (await client.get(f"/api/tasks/{t['id']}")).json()
    assert task["status"] == "CANCELLED" and task["result"] is None
    ev = await _events(client, t["id"])
    assert "TASK_CANCELLED" in ev and "TASK_COMPLETED" not in ev
    assert "TOOL_RESULT" in ev  # the work already done stays in the journal


async def test_cancel_of_task_waiting_for_approval_closes_approval(client, workspace):
    (workspace / "cancel-me.txt").write_text("x")
    t = (await client.post("/api/tasks", json={"request": "Delete the file cancel-me.txt from the workspace"})).json()["task"]
    await Worker().run_once()
    assert (await client.get(f"/api/tasks/{t['id']}")).json()["status"] == "WAITING_APPROVAL"
    await client.post(f"/api/tasks/{t['id']}/cancel")
    async with session_scope() as s:
        appr = (await s.execute(select(Approval).where(Approval.task_id == t["id"]))).scalar_one()
    assert appr.status == "REJECTED" and appr.decided_by == "system:task-cancelled"
    assert (await client.get("/api/approvals", params={"status": "PENDING"})).json()["total"] == 0
    # a later retry asks the human again instead of treating the auto-close as a human rejection
    await client.post(f"/api/tasks/{t['id']}/retry")
    await Worker().run_once()
    assert (await client.get(f"/api/tasks/{t['id']}")).json()["status"] == "WAITING_APPROVAL"
    assert (await client.get("/api/approvals", params={"status": "PENDING"})).json()["total"] == 1
    assert (workspace / "cancel-me.txt").exists()


async def test_concurrent_seeding_is_idempotent():
    from app.agents.registry import sync_builtin_agents
    from app.tasks.service import ensure_default_project

    async def seed():
        async with session_scope() as s:
            await sync_builtin_agents(s)
            await ensure_default_project(s)
        async with session_scope() as s:
            await get_tool_registry().refresh(s)

    await asyncio.gather(*(seed() for _ in range(4)))


def _serve(app, port: int) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)


async def test_local_mcp_token_auth(monkeypatch):
    from app.integrations.mcp import local_server

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    monkeypatch.setenv("LOCAL_MCP_TOKEN", "mcp-secret")
    # a StreamableHTTPSessionManager can only run once; give this second server its own
    monkeypatch.setattr(local_server.mcp, "_session_manager", None)
    _serve(local_server.build_app(), port)
    url = f"http://127.0.0.1:{port}/mcp"
    with pytest.raises(ToolProviderUnavailable):
        await MCPToolProvider("local", url).list_tools()
    tools = await MCPToolProvider("local", url, headers={"Authorization": "Bearer mcp-secret"}).list_tools()
    assert any(t.name == "get_current_time" for t in tools)
    s = Settings(local_mcp_token=SecretStr("mcp-secret"))
    local = [p for p in build_providers(s) if p.name == "local"][0]
    assert local.headers == {"Authorization": "Bearer mcp-secret"}


def test_composio_headers_from_sdk_export():
    s = Settings(composio_enabled=True, composio_mcp_url="https://backend.composio.dev/v3/mcp/x",
                 composio_mcp_headers=SecretStr('{"x-api-key": "ak_1", "x-project-id": "pr_9"}'),
                 composio_user_id="me")
    assert s.composio_configured
    prov = [p for p in build_providers(s) if p.name == "composio"][0]
    assert prov.headers == {"x-api-key": "ak_1", "x-project-id": "pr_9"}
    assert prov.url.endswith("?user_id=me")
    s2 = Settings(composio_enabled=True, composio_mcp_url="https://x", composio_api_key=SecretStr("k"))
    assert s2.composio_headers() == {"x-api-key": "k"}
    assert not Settings(composio_enabled=True, composio_mcp_url="https://x").composio_configured
