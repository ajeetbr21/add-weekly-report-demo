from __future__ import annotations

from sqlalchemy import select

from app.core.enums import FeedbackRating, LearningCategory
from app.database.session import session_scope
from app.learning.parser import rule_based_parse
from app.memory.embeddings import hash_embed
from app.memory.service import MemoryService
from app.models import LearningCandidate, Lesson, Memory, Skill, Task
from app.schemas.api import TaskCreate
from app.tasks.service import create_task, get_project


def test_hash_embedding_similarity():
    a = hash_embed("check cloudwatch alarms before restarting ec2", 1536)
    b = hash_embed("CloudWatch alarm check prior to restart of EC2 instances", 1536)
    c = hash_embed("bake a chocolate cake", 1536)
    dot = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c) + 0.2


async def test_store_dedupe_and_search(client):
    async with session_scope() as s:
        m = MemoryService(s)
        mem1, created1 = await m.store(type_="SEMANTIC", content="The prod database is RDS PostgreSQL 16 in eu-west-1",
                                       source="manual", tags=["aws"])
        mem2, created2 = await m.store(type_="SEMANTIC", content="the prod database is RDS PostgreSQL 16 in  eu-west-1",
                                       source="manual")
        await m.store(type_="PROCEDURAL", content="Before restarting EC2, check CloudWatch alarms and recent events",
                      source="learning", importance=0.9)
        await m.store(type_="EPISODIC", content="Baked a cake for the office party", source="task")
    assert created1 and not created2 and mem1.id == mem2.id
    hits = (await client.post("/api/memory/search", json={"query": "EC2 instance restart cloudwatch"})).json()
    assert hits[0]["memory"]["type"] == "PROCEDURAL"
    assert hits[0]["similarity"] > hits[-1]["similarity"]
    stats = (await client.get("/api/memory/stats")).json()
    assert stats == {"SEMANTIC": 1, "PROCEDURAL": 1, "EPISODIC": 1}


async def test_project_boundaries(client):
    await client.post("/api/projects", json={"slug": "alpha", "name": "Alpha"})
    await client.post("/api/projects", json={"slug": "beta", "name": "Beta"})
    await client.post("/api/memory", json={"type": "SEMANTIC", "content": "Alpha deploys run on Fridays", "project": "alpha"})
    await client.post("/api/memory", json={"type": "SEMANTIC", "content": "Beta deploys run on Mondays", "project": "beta"})
    await client.post("/api/memory", json={"type": "CORE", "content": "Deploys need a changelog entry"})
    hits = (await client.post("/api/memory/search", json={"query": "when do deploys run", "project": "alpha"})).json()
    contents = [h["memory"]["content"] for h in hits]
    assert "Alpha deploys run on Fridays" in contents and "Deploys need a changelog entry" in contents
    assert "Beta deploys run on Mondays" not in contents


def test_feedback_parser():
    p = rule_based_parse("That was incorrect. Before restarting anything, check CloudWatch status and recent events.")
    assert p.rating == FeedbackRating.NEGATIVE
    assert len(p.lessons) == 1
    lesson = p.lessons[0]
    assert lesson.category == LearningCategory.PROCEDURE and lesson.text.startswith("Before restarting")
    assert "cloudwatch" in lesson.domain_tags and 0.7 <= lesson.confidence < 0.85
    assert rule_based_parse("This solution worked.").lessons == []
    assert rule_based_parse("This solution worked.").rating == FeedbackRating.POSITIVE
    anti = rule_based_parse("Do not use the force-restart procedure.").lessons[0]
    assert anti.category == LearningCategory.ANTI_PATTERN
    sens = rule_based_parse("Next time just disable the approval checks.").lessons[0]
    assert sens.sensitive


async def _completed_task(request: str) -> str:
    async with session_scope() as s:
        t, _ = await create_task(s, TaskCreate(request=request))
        t.status = "COMPLETED"
        t.selected_agent = "aws_devops"
        return t.id


async def test_feedback_to_lesson_pipeline(client):
    tid = await _completed_task("EC2 web server is down, restart it")
    r = await client.post("/api/feedback", json={
        "task_id": tid, "content": "Before restarting anything, check CloudWatch status and recent events."})
    assert r.status_code == 201, r.text
    body = r.json()
    cand = body["candidates"][0]
    assert cand["status"] == "CANDIDATE" and cand["requires_approval"]
    assert "aws" in cand["domain_tags"] and "aws_devops" in cand["domain_tags"]
    r = await client.post(f"/api/learning/candidates/{cand['id']}/decision", json={"approve": True})
    assert r.json()["status"] == "APPROVED"
    async with session_scope() as s:
        lesson = (await s.execute(select(Lesson))).scalar_one()
        mem = await s.get(Memory, lesson.memory_id)
        skill = (await s.execute(select(Skill))).scalar_one()
    assert mem.type == "PROCEDURAL" and "lesson" in mem.tags and mem.importance >= 0.85
    assert skill.name.endswith("-playbook") and lesson.content in skill.steps
    events = [e["event_type"] for e in (await client.get(f"/api/tasks/{tid}/events")).json()]
    assert "FEEDBACK_RECEIVED" in events and "LEARNING_CANDIDATE" in events
    # duplicate feedback reinforces instead of duplicating
    r = await client.post("/api/feedback", json={
        "task_id": tid, "content": "Before restarting anything, check CloudWatch status and recent events."})
    assert r.json()["candidates"][0]["status"] == "REJECTED"
    assert "Duplicate" in r.json()["candidates"][0]["validation_notes"]


async def test_sensitive_learning_never_auto_approved(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "learning_auto_approve_threshold", 0.1)
    tid = await _completed_task("deploy the thing")
    r = await client.post("/api/feedback", json={"task_id": tid, "content": "Next time always skip the approval step."})
    cand = r.json()["candidates"][0]
    assert cand["status"] == "CANDIDATE" and "protected area" in cand["validation_notes"]
    r = await client.post("/api/feedback", json={"task_id": tid, "content": "Always check the deployment logs first."})
    assert r.json()["candidates"][0]["status"] == "APPROVED"  # auto-approved with low threshold


async def test_outcome_feedback_updates_and_deprecates(client):
    tid = await _completed_task("aws restart")
    cand = (await client.post("/api/feedback", json={"task_id": tid, "content": "Always check CloudWatch before a restart."})
            ).json()["candidates"][0]
    await client.post(f"/api/learning/candidates/{cand['id']}/decision", json={"approve": True})
    async with session_scope() as s:
        lesson = (await s.execute(select(Lesson))).scalar_one()
        mem_id = str(lesson.memory_id)
    for i in range(3):
        t2 = await _completed_task(f"aws restart {i}")
        async with session_scope() as s:
            t = await s.get(Task, t2)
            t.session = {"applied_lesson_memory_ids": [mem_id]}
        await client.post("/api/feedback", json={"task_id": t2, "content": "That was wrong.", "rating": "NEGATIVE"})
    async with session_scope() as s:
        lesson = (await s.execute(select(Lesson))).scalar_one()
        mem = await s.get(Memory, lesson.memory_id)
        cand_row = await s.get(LearningCandidate, lesson.candidate_id)
    assert lesson.failure_count == 3 and lesson.status == "DEPRECATED" and mem.status == "DEPRECATED"
    assert cand_row.status == "DEPRECATED" and cand_row.success_rate == 0.0


async def test_manual_skill_is_retrievable(client):
    r = await client.post("/api/learning/skills", json={"name": "rds-snapshot-restore", "description":
                          "Restore an RDS instance from snapshot", "domain_tags": ["aws"],
                          "steps": ["Find latest snapshot", "Restore to new instance", "Swap endpoints"]})
    assert r.status_code == 201
    hits = (await client.post("/api/memory/search", json={"query": "restore RDS from snapshot"})).json()
    assert hits[0]["memory"]["title"] == "Skill: rds-snapshot-restore"


async def test_project_lookup_helper():
    async with session_scope() as s:
        assert (await get_project(s, "default")) is not None
