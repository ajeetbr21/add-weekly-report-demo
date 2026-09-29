"""Health, readiness, integrations status and dashboard statistics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_token
from app.core.config import get_settings
from app.database.session import get_db, session_scope
from app.integrations.letta.client import LettaClient
from app.integrations.omniroute.client import get_model_client
from app.models import AgentRun, Approval, LearningCandidate, Lesson, Memory, SystemEvent, Task, ToolExecution
from app.schemas.api import AgentRunOut, ToolExecutionOut
from app.tools.registry import get_tool_registry

router = APIRouter(tags=["system"])
STARTED_AT = datetime.now(UTC)


@router.get("/health")
async def health() -> dict:
    """Liveness: the API process is up."""
    return {"status": "ok", "service": "atlas-api", "uptime_seconds": int((datetime.now(UTC) - STARTED_AT).total_seconds())}


async def _db_check() -> dict:
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
            rev = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
            vec = (await s.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
                   ).scalar_one_or_none()
        return {"status": "OK" if rev and vec else "NOT_MIGRATED", "migration": rev, "pgvector": vec}
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNREACHABLE", "detail": f"{type(exc).__name__}: {exc}"[:200]}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: database reachable, migrations applied, pgvector installed."""
    db = await _db_check()
    ok = db["status"] == "OK"
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready",
                                                                 "database": db})


async def integrations_status() -> dict:
    s = get_settings()
    reg = get_tool_registry()
    model = get_model_client()
    local, composio = reg.provider("local"), reg.provider("composio")
    omni, letta, local_h, comp_h = await asyncio.gather(
        model.health(), LettaClient().health(),
        local.health() if local else asyncio.sleep(0, {"status": "DISABLED"}),
        composio.health() if composio else asyncio.sleep(0, {"status": "DISABLED"}))
    return {
        "database": await _db_check(),
        "omniroute": {**omni, "base_url": s.omniroute_base_url or None,
                      "embedding_model": s.embedding_model or "local-hash-v1 (fallback)"},
        "letta": {**letta, "base_url": s.letta_base_url if s.letta_enabled else None},
        "mcp_local": {**local_h, "url": s.local_mcp_url},
        "composio": comp_h,
        "telegram": {"status": "CONFIGURED" if s.telegram_configured else "CONFIGURATION_REQUIRED",
                     "allowed_chats": len(s.telegram_allowed_chat_ids)},
        "webhooks": {"github_signature": bool(s.github_webhook_secret.get_secret_value()),
                     "custom_signature": bool(s.custom_webhook_secret.get_secret_value())},
        "auth": {"api_token_required": bool(s.atlas_api_token.get_secret_value())},
    }


@router.get("/api/integrations", dependencies=[Depends(require_api_token)])
async def integrations() -> dict:
    return await integrations_status()


@router.get("/api/system/dashboard", dependencies=[Depends(require_api_token)])
async def dashboard(db: AsyncSession = Depends(get_db)) -> dict:
    by_status = dict((await db.execute(select(Task.status, func.count()).group_by(Task.status))).all())
    since = datetime.now(UTC) - timedelta(hours=24)
    last24 = (await db.execute(select(func.count()).select_from(Task).where(Task.created_at >= since))).scalar_one()
    pending_appr = (await db.execute(select(func.count()).select_from(Approval).where(Approval.status == "PENDING"))
                    ).scalar_one()
    runs = (await db.execute(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(8))).scalars()
    tools = (await db.execute(select(ToolExecution).order_by(ToolExecution.started_at.desc()).limit(8))).scalars()
    learning = (await db.execute(select(LearningCandidate).order_by(LearningCandidate.created_at.desc()).limit(5))
                ).scalars()
    lessons = (await db.execute(select(func.count()).select_from(Lesson).where(Lesson.status == "APPROVED"))
               ).scalar_one()
    mem = dict((await db.execute(select(Memory.type, func.count()).where(Memory.status == "ACTIVE")
                                 .group_by(Memory.type))).all())
    events = (await db.execute(select(SystemEvent).order_by(SystemEvent.timestamp.desc()).limit(5))).scalars()
    recent = (await db.execute(select(Task).order_by(Task.created_at.desc()).limit(8))).scalars()
    return {
        "tasks": {"by_status": by_status, "last_24h": last24,
                  "active": sum(by_status.get(k, 0) for k in ("PENDING", "PLANNING", "RUNNING", "RETRYING",
                                                              "WAITING_APPROVAL"))},
        "recent_tasks": [{"id": t.id, "status": t.status, "source": t.source, "request": t.original_request[:140],
                          "selected_agent": t.selected_agent, "created_at": t.created_at.isoformat()} for t in recent],
        "pending_approvals": pending_appr,
        "recent_agent_runs": [AgentRunOut.model_validate(r).model_dump(mode="json") for r in runs],
        "recent_tool_executions": [ToolExecutionOut.model_validate(t).model_dump(mode="json", exclude={"result"})
                                   for t in tools],
        "recent_learning": [{"id": str(c.id), "lesson": c.normalized_lesson, "status": c.status,
                             "category": c.category, "created_at": c.created_at.isoformat()} for c in learning],
        "approved_lessons": lessons,
        "memory": mem,
        "system_events": [{"ts": e.timestamp.isoformat(), "component": e.component, "event": e.event,
                           "message": e.message} for e in events],
    }
