"""Scheduler, webhooks, Telegram handlers and Letta session runtime."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from app.database.session import session_scope
from app.integrations.letta.client import LettaClient
from app.integrations.telegram.handlers import Ctx, format_result, handle_callback, handle_message
from app.memory.session import SessionRuntime
from app.models import ScheduledTask, Task
from app.scheduler import service as scheduler


# ------------------------------------------------------------------------------------------ scheduler
async def test_schedule_once_fires_exactly_once(client):
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r = await client.post("/api/schedules", json={"name": "one-off", "request": "Check AWS cost report",
                                                  "schedule_type": "ONCE", "run_at": past})
    assert r.status_code == 201, r.text
    async with session_scope() as s:
        fired = await scheduler.tick(s)
    async with session_scope() as s:
        fired2 = await scheduler.tick(s)
        sched = (await s.execute(select(ScheduledTask))).scalar_one()
        task = await s.get(Task, fired[0])
    assert len(fired) == 1 and fired2 == []
    assert task.source == "scheduler" and task.idempotency_key.startswith("schedule:")
    assert sched.enabled is False and sched.run_count == 1 and sched.last_task_id == task.id


async def test_recurring_schedules_and_idempotency(client):
    anchor = datetime(2026, 1, 5, 7, 30, tzinfo=UTC)  # a Monday
    r = await client.post("/api/schedules", json={"name": "daily", "request": "Summarize AWS alarms",
                                                  "schedule_type": "DAILY", "run_at": anchor.isoformat()})
    daily = r.json()
    assert daily["cron_expression"] == "30 7 * * *"
    assert datetime.fromisoformat(daily["next_run_at"]) > datetime.now(UTC)
    w = (await client.post("/api/schedules", json={"name": "weekly", "request": "x", "schedule_type": "WEEKLY",
                                                   "run_at": anchor.isoformat()})).json()
    assert w["cron_expression"] == "30 7 * * 1"
    m = (await client.post("/api/schedules", json={"name": "monthly", "request": "x", "schedule_type": "MONTHLY",
                                                   "run_at": anchor.isoformat()})).json()
    assert m["cron_expression"] == "30 7 5 * *"
    bad = await client.post("/api/schedules", json={"name": "c", "request": "x", "schedule_type": "CRON",
                                                    "cron_expression": "not a cron"})
    assert bad.status_code == 400
    c = (await client.post("/api/schedules", json={"name": "c", "request": "cron job", "schedule_type": "CRON",
                                                   "cron_expression": "*/5 * * * *"})).json()
    # force it due twice for the same fire time -> only one task (idempotency key)
    fire = datetime.now(UTC) - timedelta(seconds=1)
    for _ in range(2):
        async with session_scope() as s:
            row = await s.get(ScheduledTask, __import__("uuid").UUID(c["id"]))
            row.next_run_at = fire
        async with session_scope() as s:
            await scheduler.tick(s)
    async with session_scope() as s:
        tasks = (await s.execute(select(Task).where(Task.source == "scheduler"))).scalars().all()
    assert len(tasks) == 1
    r = await client.post(f"/api/schedules/{daily['id']}/run-now")
    assert r.json()["task"]["source"] == "scheduler"


# ------------------------------------------------------------------------------------------ webhooks
def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_github_webhook_signature_and_idempotency(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SecretStr("whsec"))
    payload = json.dumps({"action": "opened", "repository": {"full_name": "me/repo"},
                          "pull_request": {"number": 7, "title": "Fix bug", "html_url": "https://github.com/me/repo/pull/7"}}
                         ).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1", "Content-Type": "application/json"}
    r = await client.post("/api/webhooks/github", content=payload, headers={**headers, "X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401
    r1 = await client.post("/api/webhooks/github", content=payload, headers={**headers, "X-Hub-Signature-256": _sig("whsec", payload)})
    r2 = await client.post("/api/webhooks/github", content=payload, headers={**headers, "X-Hub-Signature-256": _sig("whsec", payload)})
    assert r1.status_code == 202 and r1.json()["duplicate"] is False and r1.json()["task_id"]
    assert r2.json()["duplicate"] is True and r2.json()["task_id"] == r1.json()["task_id"]
    task = (await client.get(f"/api/tasks/{r1.json()['task_id']}")).json()
    assert task["source"] == "webhook" and "#7" in task["original_request"]
    ping = json.dumps({"zen": "hi"}).encode()
    r = await client.post("/api/webhooks/github", content=ping, headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d-2",
                                                                        "X-Hub-Signature-256": _sig("whsec", ping)})
    assert r.json()["task_id"] is None


async def test_custom_webhook(client, settings, monkeypatch):
    body = {"event_id": "evt-1", "request": "Nightly backup failed on db-1, investigate", "priority": 2}
    r1 = await client.post("/api/webhooks/custom", json=body)
    r2 = await client.post("/api/webhooks/custom", json=body)
    assert r1.status_code == 202 and r2.json()["duplicate"] and r1.json()["task_id"] == r2.json()["task_id"]
    assert (await client.post("/api/webhooks/custom", json={"event_id": "x"})).status_code == 422
    monkeypatch.setattr(settings, "custom_webhook_secret", SecretStr("abc"))
    raw = json.dumps({"event_id": "evt-2", "request": "hello there"}).encode()
    assert (await client.post("/api/webhooks/custom", content=raw)).status_code == 401
    r = await client.post("/api/webhooks/custom", content=raw, headers={"X-Atlas-Signature": _sig("abc", raw)})
    assert r.status_code == 202


# ------------------------------------------------------------------------------------------ telegram
class FakeApi:
    def __init__(self):
        self.created: list[str] = []
        self.decisions: list[tuple[str, bool]] = []
        self.feedbacks: list[tuple[str, str]] = []
        self.polls = 0

    async def create_task(self, request, chat_id, user):
        self.created.append(request)
        return {"id": "TASK-2026-000009"}

    async def get_task(self, task_id):
        self.polls += 1
        if self.polls == 1:
            return {"id": task_id, "status": "WAITING_APPROVAL"}
        return {"id": task_id, "status": "COMPLETED", "result": {"summary": "All good", "findings": ["f1"],
                                                                 "configuration_required": ["CONFIGURATION REQUIRED: x"]}}

    async def approvals(self, task_id=None, status="PENDING"):
        return [{"id": "a-1", "task_id": "TASK-2026-000009", "action_summary": "delete_workspace_file(path=x)",
                 "risk_level": "DESTRUCTIVE"}]

    async def decide(self, approval_id, approve, who):
        self.decisions.append((approval_id, approve))
        return {"status": "APPROVED" if approve else "REJECTED", "action_summary": "delete x"}

    async def feedback(self, task_id, content, chat_id, rating=None):
        self.feedbacks.append((task_id, content))
        return {"candidates": [{"id": "c"}]}

    async def list_agents(self):
        return [{"id": "aws_devops", "name": "AWS / DevOps Agent", "status": "ACTIVE", "description": "d"}]

    async def list_tasks(self, limit=5):
        return [{"id": "TASK-2026-000001", "status": "COMPLETED", "original_request": "hello"}]

    async def dashboard(self):
        return {"tasks": {"by_status": {"COMPLETED": 3}, "active": 1}, "pending_approvals": 0, "approved_lessons": 2}

    async def health(self):
        return {"status": "ready"}


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[int, str, Any]] = []
        self.answers: list[str] = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))

    async def answer_callback(self, callback_id, text):
        self.answers.append(text)


def _msg(text: str, chat: int = 42) -> dict:
    return {"chat": {"id": chat}, "text": text, "from": {"username": "owner"}}


async def test_telegram_commands_and_task_flow():
    api, sender = FakeApi(), FakeSender()
    ctx = Ctx(api=api, sender=sender, allowed_chat_ids=[42], poll_seconds=0.01)
    await handle_message(ctx, _msg("/start"))
    assert "/status" in sender.sent[-1][1]
    await handle_message(ctx, _msg("/agents"))
    assert "AWS / DevOps Agent" in sender.sent[-1][1]
    await handle_message(ctx, _msg("/status"))
    assert "Pending approvals: 0" in sender.sent[-1][1]
    await handle_message(ctx, _msg("/tasks"))
    assert "TASK-2026-000001" in sender.sent[-1][1]
    follow = await handle_message(ctx, _msg("Check today's AWS alarms"))
    await follow
    texts = [t for _, t, _ in sender.sent]
    assert api.created == ["Check today's AWS alarms"]
    assert any("Approval required" in t for t in texts)
    approval_msg = next(s for s in sender.sent if "Approval required" in s[1])
    assert approval_msg[2][0][0]["callback_data"] == "appr:a-1:y"
    assert "All good" in texts[-1] and "CONFIGURATION REQUIRED" in texts[-1]
    await handle_callback(ctx, {"id": "cb1", "data": "appr:a-1:y", "message": {"chat": {"id": 42}}, "from": {"username": "o"}})
    assert api.decisions == [("a-1", True)] and sender.answers[-1] == "APPROVED"
    await handle_callback(ctx, {"id": "cb2", "data": "fb:TASK-2026-000009:n", "message": {"chat": {"id": 42}}})
    assert api.feedbacks[-1] == ("TASK-2026-000009", "That was incorrect.")
    await handle_message(ctx, _msg("/feedback TASK-2026-000009 Use CloudWatch before restarting next time"))
    assert api.feedbacks[-1][1] == "Use CloudWatch before restarting next time"


async def test_telegram_rejects_unknown_chat():
    api, sender = FakeApi(), FakeSender()
    ctx = Ctx(api=api, sender=sender, allowed_chat_ids=[1])
    await handle_message(ctx, _msg("delete everything", chat=666))
    assert api.created == [] and "not authorized" in sender.sent[-1][1]


async def test_telegram_through_real_api(client):
    """The bot's API client against the real FastAPI app (no business logic in the bot)."""
    from app.integrations.telegram.api_client import AtlasApiClient
    from app.main import create_app

    api = AtlasApiClient(transport=httpx.ASGITransport(app=create_app()))
    api._client.base_url = "http://test"
    task = await api.create_task("Organize my notes", 42, "owner")
    assert task["source"] == "telegram" and task["session_key"] == "telegram:42"
    assert (await api.list_agents())
    assert format_result({**(await api.get_task(task["id"])), "result": {"summary": "ok"}}).endswith("ok")
    await api.close()


# ------------------------------------------------------------------------------------------ letta
class FakeLetta:
    def __init__(self):
        self.blocks: dict[str, dict] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v1/health/":
            return httpx.Response(200, json={"version": "0.test", "status": "ok"})
        if path == "/v1/blocks/" and req.method == "POST":
            body = json.loads(req.content)
            bid = f"block-{len(self.blocks)}"
            self.blocks[bid] = {"id": bid, **body}
            return httpx.Response(200, json=self.blocks[bid])
        bid = path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(200, json=self.blocks[bid])
        if req.method == "PATCH":
            self.blocks[bid].update(json.loads(req.content))
            return httpx.Response(200, json=self.blocks[bid])
        return httpx.Response(404)


async def test_letta_session_runtime(settings):
    fake = FakeLetta()
    s2 = settings.model_copy(update={"letta_enabled": True, "letta_base_url": "http://letta.test"})
    letta = LettaClient(settings=s2, transport=httpx.MockTransport(fake.handler))
    assert (await letta.health())["status"] == "OK"
    async with session_scope() as s:
        rt = SessionRuntime(s, letta)
        used = await rt.record_exchange("web:me", "TASK-1", "check alarms", "2 alarms need attention")
        state = await rt.load("web:me")
    assert used == "letta" and state.runtime == "letta"
    assert state.conversation[0]["response"] == "2 alarms need attention"
    assert {b["label"] for b in fake.blocks.values()} == {"conversation", "active_task", "scratchpad"}


async def test_letta_failure_falls_back_to_local(settings):
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    s2 = settings.model_copy(update={"letta_enabled": True, "letta_base_url": "http://letta.test"})
    letta = LettaClient(settings=s2, transport=httpx.MockTransport(down))
    async with session_scope() as s:
        rt = SessionRuntime(s, letta)
        used = await rt.record_exchange("web:x", "TASK-2", "hello there", "hi")
        state = await rt.load("web:x")
    assert used == "local" and state.runtime == "local" and state.warning
    assert state.conversation[0]["request"] == "hello there"
