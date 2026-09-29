"""End-to-end: User -> Task -> Orchestrator -> Agent -> Model -> Tool (real MCP) -> Result -> Memory ->
Feedback -> Learning -> future task retrieves the lesson. Plus approval pause/resume and crash recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database.session import session_scope
from app.models import AgentRun, Memory, Task, TaskStep, ToolExecution
from app.orchestrator.orchestrator import Orchestrator
from app.worker import Worker
from tests.helpers import ScriptedGateway, final, tool_call

DEMO = "Analyze today's AWS alarms and summarize anything that needs attention."


async def _events(client, task_id):
    return [e["event_type"] for e in (await client.get(f"/api/tasks/{task_id}/events")).json()]


async def test_demo_flow_offline_with_learning(client):
    # 1. user submits the task from the web UI
    t = (await client.post("/api/tasks", json={"request": DEMO, "source": "web", "user_external_id": "me"})).json()["task"]
    # 2-13. worker executes it through the orchestrator
    done = await Worker().run_once()
    assert done == [t["id"]]
    task = (await client.get(f"/api/tasks/{t['id']}")).json()
    assert task["status"] == "COMPLETED", task
    assert task["selected_agent"] == "aws_devops"
    res = task["result"]
    assert res["offline"] is True
    assert any("Composio" in c for c in res["configuration_required"])  # honest: AWS tools not configured
    assert res["steps"][0]["tools_used"][0]["tool_id"] == "local:get_current_time"
    assert res["memory"]["stored"] >= 1 and res["session_runtime"] == "local"
    ev = await _events(client, t["id"])
    for e in ("TASK_CREATED", "TASK_CLAIMED", "TASK_NORMALIZED", "MEMORY_RETRIEVED", "PLAN_CREATED", "AGENT_SELECTED",
              "STEP_STARTED", "AGENT_STARTED", "MODEL_CALL", "TOOL_CALLED", "TOOL_RESULT", "VALIDATION",
              "STEP_COMPLETED", "MEMORY_CREATED", "TASK_COMPLETED"):
        assert e in ev, e
    execs = (await client.get(f"/api/tasks/{t['id']}/tool-executions")).json()
    assert execs[0]["status"] == "SUCCESS"
    # 14-15. feedback -> learning candidate -> approved lesson
    fb = (await client.post("/api/feedback", json={"task_id": t["id"], "content":
          "Before restarting anything, check CloudWatch status and recent events."})).json()
    cand = fb["candidates"][0]
    await client.post(f"/api/learning/candidates/{cand['id']}/decision", json={"approve": True})
    # 16. a similar future task retrieves and applies the learned procedure
    t2 = (await client.post("/api/tasks", json={"request": "The EC2 app server is down after the AWS alarm fired. "
                                                          "Should we restart it?"})).json()["task"]
    await Worker().run_once()
    task2 = (await client.get(f"/api/tasks/{t2['id']}")).json()
    assert task2["status"] == "COMPLETED" and task2["selected_agent"] == "aws_devops"
    applied = task2["result"]["applied_lessons"]
    assert applied and "CloudWatch" in applied[0]["content"]
    assert any("Apply learned procedure" in r for r in task2["result"]["steps"][0]["recommendations"])
    assert any("Learned procedure" in g for g in task2["plan"]["guidance"])


async def test_llm_mode_end_to_end():
    calls = {"n": 0}

    def responder(body):
        calls["n"] += 1
        if body.get("response_format"):  # planner
            return {"role": "assistant", "content": '{"complexity":"simple","needs_report":true,"rationale":"aws",'
                                                    '"steps":[{"agent_id":"aws_devops","objective":"Check alarms",'
                                                    '"depends_on":[]}]}'}
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return tool_call("local__get_current_time", {"timezone": "UTC"})
        return final("No alarms need attention today.", findings=["Time checked via local tool"],
                     facts=["The AWS account uses CloudWatch alarms for the web tier."])

    gw = ScriptedGateway(responder)
    async with session_scope() as s:
        from app.schemas.api import TaskCreate
        from app.tasks.service import create_task

        t, _ = await create_task(s, TaskCreate(request=DEMO))
    await Worker(orchestrator=Orchestrator(model=gw.client())).run_once()
    async with session_scope() as s:
        task = await s.get(Task, t.id)
        run = (await s.execute(select(AgentRun).where(AgentRun.task_id == t.id))).scalar_one()
        execs = (await s.execute(select(ToolExecution).where(ToolExecution.task_id == t.id))).scalars().all()
        facts = (await s.execute(select(Memory).where(Memory.type == "SEMANTIC"))).scalars().all()
    assert task.status == "COMPLETED" and task.result["offline"] is False
    assert task.result["summary"] == "No alarms need attention today."
    assert task.plan["planner"] == "llm" and task.result.get("report_id")
    assert run.model == "test-reasoning" and run.model_provider == "omniroute" and run.iterations == 2
    assert run.prompt_tokens > 0 and [e.tool_id for e in execs] == ["local:get_current_time"]
    assert facts and "CloudWatch" in facts[0].content
    tool_msgs = [m for m in gw.requests[-1]["body"]["messages"] if m["role"] == "tool"]
    assert '"status": "success"' in tool_msgs[0]["content"]


async def test_approval_pause_and_resume(client, workspace):
    (workspace / "old-report.txt").write_text("stale")
    t = (await client.post("/api/tasks", json={"request": "Delete the file old-report.txt from the workspace"})).json()["task"]
    await Worker().run_once()
    task = (await client.get(f"/api/tasks/{t['id']}")).json()
    assert task["status"] == "WAITING_APPROVAL" and (workspace / "old-report.txt").exists()
    appr = (await client.get("/api/approvals", params={"status": "PENDING"})).json()["items"][0]
    assert appr["risk_level"] == "DESTRUCTIVE" and "old-report.txt" in appr["action_summary"]
    await client.post(f"/api/approvals/{appr['id']}/decision", json={"approve": True, "decided_by": "owner"})
    assert (await client.get(f"/api/tasks/{t['id']}")).json()["status"] == "PENDING"
    await Worker().run_once()
    task = (await client.get(f"/api/tasks/{t['id']}")).json()
    assert task["status"] == "COMPLETED" and not (workspace / "old-report.txt").exists()
    execs = (await client.get(f"/api/tasks/{t['id']}/tool-executions")).json()
    statuses = [e["status"] for e in execs if e["tool_id"] == "local:delete_workspace_file"]
    assert statuses == ["WAITING_APPROVAL", "SUCCESS"]
    assert all(e["risk_level"] == "READ" for e in execs if e["tool_id"] != "local:delete_workspace_file")
    ev = await _events(client, t["id"])
    assert "APPROVAL_REQUESTED" in ev and "APPROVAL_DECIDED" in ev


async def test_multi_agent_task_with_report(client):
    t = (await client.post("/api/tasks", json={"request": "Investigate this AWS EC2 problem, research the "
                                                         "documentation, and prepare a report."})).json()["task"]
    await Worker().run_once()
    task = (await client.get(f"/api/tasks/{t['id']}")).json()
    assert task["status"] == "COMPLETED"
    assert [s["agent_id"] for s in task["steps"]] == ["aws_devops", "research"]
    report = (await client.get(f"/api/tasks/{t['id']}/report")).json()
    assert report and "## Executive summary" in report["markdown"] and report["content"]["agents"] == [
        "aws_devops", "research"]


async def test_crash_recovery_resumes_without_rerunning_completed_steps(client):
    t = (await client.post("/api/tasks", json={"request": "Investigate this AWS EC2 problem, research the "
                                                         "documentation, and prepare a report."})).json()["task"]
    orch = Orchestrator(worker_id="deadhost:1:abc")
    async with session_scope() as s:
        from app.tasks.service import claim_next_task

        await claim_next_task(s, "deadhost:1:abc", 60)
    assert await orch._prepare(t["id"])
    await orch._run_step(t["id"], 0)
    # simulate the worker dying in the middle of step 1
    async with session_scope() as s:
        task = await s.get(Task, t["id"])
        task.status = "RUNNING"
        task.locked_by = "deadhost:1:abc"
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        step1 = (await s.execute(select(TaskStep).where(TaskStep.task_id == t["id"], TaskStep.step_index == 1))
                 ).scalar_one()
        step1.status, step1.attempts = "RUNNING", 1
    w = Worker()
    recovered = await w.recover()
    assert recovered == [t["id"]]
    assert (await client.get(f"/api/tasks/{t['id']}")).json()["status"] == "RETRYING"
    await w.run_once()
    task = (await client.get(f"/api/tasks/{t['id']}")).json()
    assert task["status"] == "COMPLETED" and task["retry_count"] == 1
    steps = {s["step_index"]: s for s in task["steps"]}
    assert steps[0]["attempts"] == 1 and steps[1]["attempts"] == 2
    assert "RECOVERED" in await _events(client, t["id"])


async def test_startup_recovery_of_own_host(client):
    import socket

    t = (await client.post("/api/tasks", json={"request": "Organize my notes"})).json()["task"]
    async with session_scope() as s:
        task = await s.get(Task, t["id"])
        task.status, task.locked_by = "PLANNING", f"{socket.gethostname()}:999:old"
        task.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)  # lease NOT expired yet
    w = Worker()
    assert await w.recover(startup=True) == [t["id"]]
