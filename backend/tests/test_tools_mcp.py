from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.enums import RiskLevel
from app.database.session import session_scope
from app.models import Approval, Task, ToolExecution
from app.schemas.api import ApprovalDecision, TaskCreate
from app.services.approvals import decide
from app.tasks.service import create_task
from app.tools.base import RawToolResult, ToolSpec, classify_risk
from app.tools.executor import ApprovalRequired, ToolContext, ToolExecutor
from app.tools.normalizer import normalize
from app.tools.registry import ToolRegistry, get_tool_registry


async def _task() -> str:
    async with session_scope() as s:
        t, _ = await create_task(s, TaskCreate(request="tool test task"))
        return t.id


async def test_local_mcp_discovery_and_risk(client):
    tools = {t["id"]: t for t in (await client.get("/api/tools")).json()}
    assert "local:get_current_time" in tools and "local:http_fetch" in tools
    assert tools["local:get_current_time"]["risk_level"] == "READ"
    assert tools["local:write_workspace_note"]["risk_level"] == "WRITE"
    assert tools["local:delete_workspace_file"]["risk_level"] == "DESTRUCTIVE"
    assert tools["local:delete_workspace_file"]["requires_approval"] is True
    assert tools["local:http_fetch"]["toolkit"] == "web"
    status = get_tool_registry().provider_status
    assert status["local"]["status"] == "OK"
    assert status["composio"]["status"] == "CONFIGURATION_REQUIRED"


def test_risk_heuristics():
    assert classify_risk("AWS_EC2_TERMINATE_INSTANCES") == RiskLevel.DESTRUCTIVE
    assert classify_risk("GITHUB_CREATE_AN_ISSUE") == RiskLevel.WRITE
    assert classify_risk("AWS_CLOUDWATCH_DESCRIBE_ALARMS") == RiskLevel.READ
    assert classify_risk("anything", {"readOnlyHint": True}) == RiskLevel.READ
    assert classify_risk("get_x", {"destructiveHint": True}) == RiskLevel.DESTRUCTIVE


def test_normalizer_formats():
    spec = ToolSpec(id="composio:GITHUB_X", name="GITHUB_X", provider="composio", toolkit="github")
    raw = RawToolResult(content=[{"type": "text", "text": '{"successful": false, "error": "bad creds", "data": {}}'}])
    n = normalize(spec, raw)
    assert n.status == "error" and "bad creds" in n.errors[0]
    raw = RawToolResult(content=[{"type": "text", "text": "see https://docs.aws.amazon.com/x"}])
    n = normalize(spec, raw)
    assert n.status == "success" and n.evidence[0]["url"].startswith("https://docs.aws")
    assert set(n.model_dump()) == {"status", "summary", "data", "evidence", "errors", "metadata"}


async def test_execute_read_tool_is_recorded():
    task_id = await _task()
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:get_current_time")
        res = await ToolExecutor(s, reg).execute(ToolContext(task_id, 0, "general"), spec, {"timezone": "UTC"})
    assert res.status == "success" and "iso" in res.data
    async with session_scope() as s:
        rows = (await s.execute(select(ToolExecution).where(ToolExecution.task_id == task_id))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "SUCCESS" and rows[0].duration_ms is not None


async def test_invalid_arguments_blocked():
    task_id = await _task()
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:read_workspace_file")
        res = await ToolExecutor(s, reg).execute(ToolContext(task_id, 0, "coding"), spec, {})
    assert res.status == "error" and "missing required argument 'path'" in res.summary


async def test_destructive_tool_requires_approval_then_executes(workspace):
    (workspace / "old.log").write_text("x")
    task_id = await _task()
    ctx = ToolContext(task_id, 0, "general")
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:delete_workspace_file")
        with pytest.raises(ApprovalRequired) as exc:
            await ToolExecutor(s, reg).execute(ctx, spec, {"path": "old.log", "api_token": "secret-value"})
        approval_id = exc.value.approval_id
    assert (workspace / "old.log").exists()
    async with session_scope() as s:
        appr = await s.get(Approval, approval_id)
        assert appr.status == "PENDING" and appr.arguments["api_token"] == "***REDACTED***"
        await decide(s, approval_id, ApprovalDecision(approve=True, decided_by="tester"))
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:delete_workspace_file")
        res = await ToolExecutor(s, reg).execute(ctx, spec, {"path": "old.log", "api_token": "secret-value"})
    assert res.status == "success" and not (workspace / "old.log").exists()
    async with session_scope() as s:
        statuses = [r.status for r in (await s.execute(select(ToolExecution).where(
            ToolExecution.task_id == task_id).order_by(ToolExecution.started_at))).scalars()]
    assert statuses == ["WAITING_APPROVAL", "SUCCESS"]


async def test_rejected_action_not_executed(workspace, client):
    (workspace / "keep.txt").write_text("keep")
    task_id = await _task()
    ctx = ToolContext(task_id, 0, "general")
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:delete_workspace_file")
        with pytest.raises(ApprovalRequired) as exc:
            await ToolExecutor(s, reg).execute(ctx, spec, {"path": "keep.txt"})
    r = await client.post(f"/api/approvals/{exc.value.approval_id}/decision",
                          json={"approve": False, "reason": "no way", "decided_by": "me"})
    assert r.json()["status"] == "REJECTED"
    assert (await client.post(f"/api/approvals/{exc.value.approval_id}/decision",
                              json={"approve": True})).status_code == 409
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:delete_workspace_file")
        res = await ToolExecutor(s, reg).execute(ctx, spec, {"path": "keep.txt"})
    assert res.status == "rejected" and (workspace / "keep.txt").exists()


async def test_tool_call_is_written_ahead_and_completed():
    """The execution row is committed as RUNNING before the provider is called, then completed."""
    task_id = await _task()
    seen: list[str] = []
    reg = get_tool_registry()
    provider = reg.provider("local")
    original = provider.call_tool

    async def spy(spec, arguments):
        async with session_scope() as other:  # a different connection sees the write-ahead record
            rows = (await other.execute(select(ToolExecution).where(ToolExecution.task_id == task_id))).scalars().all()
            seen.extend(r.status for r in rows)
        return await original(spec, arguments)

    provider.call_tool = spy  # type: ignore[method-assign]
    try:
        async with session_scope() as s:
            spec = await reg.get(s, "local:get_current_time")
            await ToolExecutor(s, reg).execute(ToolContext(task_id, 0, "general"), spec, {})
    finally:
        provider.call_tool = original  # type: ignore[method-assign]
    async with session_scope() as s:
        row = (await s.execute(select(ToolExecution).where(ToolExecution.task_id == task_id))).scalar_one()
    assert seen == ["RUNNING"] and row.status == "SUCCESS" and row.duration_ms is not None


async def test_completed_mutation_is_replayed_not_reexecuted(workspace):
    task_id = await _task()
    ctx = ToolContext(task_id, 0, "general")
    args = {"filename": "replay.txt", "content": "one"}
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:write_workspace_note")
        await ToolExecutor(s, reg).execute(ctx, spec, args)
    (workspace / "notes" / "replay.txt").write_text("changed by someone else")
    async with session_scope() as s:  # e.g. the step is re-run after a crash
        reg = get_tool_registry()
        spec = await reg.get(s, "local:write_workspace_note")
        res = await ToolExecutor(s, reg).execute(ctx, spec, args)
    assert res.status == "success" and res.metadata.get("replayed_from")
    assert (workspace / "notes" / "replay.txt").read_text() == "changed by someone else"


async def test_interrupted_mutation_needs_human_acknowledgement(workspace):
    from app.tasks.service import mark_interrupted, retry_task
    from app.tools.executor import args_hash

    task_id = await _task()
    args = {"filename": "interrupted.txt", "content": "x"}
    async with session_scope() as s:  # a worker died while this WRITE call was running
        s.add(ToolExecution(task_id=task_id, step_index=0, agent_id="general", tool_id="local:write_workspace_note",
                            arguments=args, arguments_hash=args_hash(args), risk_level="WRITE", status="RUNNING"))
    async with session_scope() as s:
        assert (await mark_interrupted(s, task_id, "worker interrupted"))["tool_executions"] == 1
    ctx = ToolContext(task_id, 0, "general")
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:write_workspace_note")
        res = await ToolExecutor(s, reg).execute(ctx, spec, args)
    assert res.status == "error" and "interrupted" in res.summary
    assert not (workspace / "notes" / "interrupted.txt").exists()
    async with session_scope() as s:  # the human retries the task -> acknowledged -> may run again
        t = await s.get(Task, task_id)
        t.status = "FAILED"
    async with session_scope() as s:
        await retry_task(s, task_id)
    async with session_scope() as s:
        reg = get_tool_registry()
        spec = await reg.get(s, "local:write_workspace_note")
        res = await ToolExecutor(s, reg).execute(ctx, spec, args)
    assert res.status == "success" and (workspace / "notes" / "interrupted.txt").exists()


async def test_unconfigured_provider_is_configuration_required():
    task_id = await _task()
    spec = ToolSpec(id="composio:AWS_CLOUDWATCH_DESCRIBE_ALARMS", name="AWS_CLOUDWATCH_DESCRIBE_ALARMS",
                    provider="composio", toolkit="aws")
    async with session_scope() as s:
        res = await ToolExecutor(s, get_tool_registry()).execute(ToolContext(task_id, 0, "aws_devops"), spec, {})
        row = (await s.execute(select(ToolExecution).where(ToolExecution.task_id == task_id))).scalar_one()
    assert res.status == "configuration_required" and row.status == "CONFIGURATION_REQUIRED"


def test_agent_tool_scoping():
    specs = [ToolSpec(id=i, name=i.split(":")[1], provider=i.split(":")[0]) for i in
             ("local:http_fetch", "local:delete_workspace_file", "composio:AWS_EC2_DESCRIBE", "composio:GITHUB_X")]
    got = [s.id for s in ToolRegistry.for_agent(["composio:AWS*", "local:http_fetch"], specs)]
    assert got == ["local:http_fetch", "composio:AWS_EC2_DESCRIBE"]


async def test_task_model_unused_import_guard():
    assert Task.__tablename__ == "tasks"
