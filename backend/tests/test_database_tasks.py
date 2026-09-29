from __future__ import annotations

from sqlalchemy import text

from app.core.logging import REDACTED, redact
from app.database.session import session_scope


async def test_migrations_and_pgvector():
    async with session_scope() as s:
        rev = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        vec = (await s.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))).scalar_one()
        dist = (await s.execute(text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector"))).scalar_one()
        idx = (await s.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_memory_embeddings_hnsw'"))
               ).scalar_one()
    assert rev == "0001"
    assert vec
    assert abs(dist - 1.0) < 1e-6
    assert "hnsw" in idx


def test_redaction():
    data = {"api_key": "abc", "nested": {"password": "p", "ok": "Bearer abcdefghijklmnop"}, "list": ["AKIAABCDEFGHIJKLMNOP"]}
    out = redact(data)
    assert out["api_key"] == REDACTED and out["nested"]["password"] == REDACTED
    assert REDACTED in out["nested"]["ok"] and out["list"][0] == REDACTED
    # token *counts* are metadata, not secrets: they must stay readable in the journal
    kept = redact({"usage": {"prompt_tokens": 1902, "completion_tokens": 64}, "max_tokens": 512,
                   "bot_token": "8000000:AAA"})
    assert kept["usage"] == {"prompt_tokens": 1902, "completion_tokens": 64}
    assert kept["max_tokens"] == 512 and kept["bot_token"] == REDACTED


async def test_health_and_ready(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    r = await client.get("/ready")
    assert r.status_code == 200 and r.json()["database"]["pgvector"]


async def test_create_task_and_journal(client):
    r = await client.post("/api/tasks", json={"request": "  Check   today's AWS alarms  ", "source": "web"})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    assert task["id"].startswith("TASK-") and len(task["id"]) == len("TASK-2026-000001")
    assert task["status"] == "PENDING" and task["source"] == "web"
    ev = (await client.get(f"/api/tasks/{task['id']}/events")).json()
    assert ev[0]["event_type"] == "TASK_CREATED"


async def test_task_idempotency_and_pagination(client):
    body = {"request": "do something useful", "idempotency_key": "abc-1"}
    a = (await client.post("/api/tasks", json=body)).json()
    b = (await client.post("/api/tasks", json=body)).json()
    assert a["task"]["id"] == b["task"]["id"] and b["created"] is False
    for i in range(3):
        await client.post("/api/tasks", json={"request": f"task number {i}"})
    page = (await client.get("/api/tasks", params={"limit": 2, "offset": 0})).json()
    assert page["total"] == 4 and len(page["items"]) == 2


async def test_validation_and_not_found_errors(client):
    r = await client.post("/api/tasks", json={"request": "   "})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = await client.get("/api/tasks/TASK-2026-999999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_cancel_and_retry(client):
    t = (await client.post("/api/tasks", json={"request": "cancel me please"})).json()["task"]
    r = await client.post(f"/api/tasks/{t['id']}/cancel")
    assert r.json()["status"] == "CANCELLED"
    assert (await client.post(f"/api/tasks/{t['id']}/cancel")).status_code == 409
    r = await client.post(f"/api/tasks/{t['id']}/retry")
    assert r.json()["status"] == "RETRYING"


async def test_csrf_origin_guard(client):
    r = await client.post("/api/tasks", json={"request": "do evil things"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_origin"
    r = await client.post("/api/tasks", content='{"request": "hello there"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = await client.post("/api/tasks", json={"request": "hello there"}, headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 201
    assert r.headers.get("x-correlation-id")
    # webhooks authenticate themselves and are not subject to the browser origin check
    r = await client.post("/api/webhooks/custom", json={"event_id": "o-1", "request": "from a tunnel"},
                          headers={"Origin": "https://github.com"})
    assert r.status_code == 202


async def test_api_token_enforced(client, settings, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "atlas_api_token", SecretStr("s3cret-token"))
    assert (await client.get("/api/tasks")).status_code == 401
    assert (await client.get("/api/tasks", headers={"Authorization": "Bearer s3cret-token"})).status_code == 200
    assert (await client.get("/api/tasks", headers={"X-API-Key": "s3cret-token"})).status_code == 200
    assert (await client.get("/health")).status_code == 200


async def test_project_detection(client):
    await client.post("/api/projects", json={"slug": "aws-ops", "name": "AWS Operations",
                                             "keywords": ["aws", "cloudwatch", "ec2", "rds"]})
    t = (await client.post("/api/tasks", json={"request": "Investigate the RDS lifecycle"})).json()["task"]
    projects = {p["id"]: p["slug"] for p in (await client.get("/api/projects")).json()}
    assert projects[t["project_id"]] == "aws-ops"
    t2 = (await client.post("/api/tasks", json={"request": "Write a haiku"})).json()["task"]
    assert projects[t2["project_id"]] == "default"
