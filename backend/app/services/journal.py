"""Task Journal: append-only event log from which any task's full history can be reconstructed."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import EventType
from app.core.logging import log_event, redact
from app.database.session import session_scope
from app.models import AuditLog, SystemEvent, TaskEvent

logger = logging.getLogger("atlas.journal")


async def record(
    session: AsyncSession,
    task_id: str,
    event_type: EventType | str,
    message: str | None = None,
    *,
    agent_id: str | None = None,
    step_index: int | None = None,
    **metadata: Any,
) -> TaskEvent:
    clean = redact(metadata)
    ev = TaskEvent(task_id=task_id, event_type=str(event_type), message=message, agent_id=agent_id,
                   step_index=step_index, extra=clean)
    session.add(ev)
    await session.flush()
    log_event(logger, str(event_type), task_id=task_id, agent_id=agent_id, step=step_index,
              message=(message or "")[:300], **{k: v for k, v in clean.items() if k in ("status", "duration_ms",
                                                                                         "tool_id", "model")})
    return ev


async def record_now(task_id: str, event_type: EventType | str, message: str | None = None, *,
                     agent_id: str | None = None, step_index: int | None = None, **metadata: Any) -> None:
    """Autonomous journal write in its own transaction, committed immediately. Used inside long-running
    agent steps so progress is visible live and survives a worker crash."""
    async with session_scope() as s:
        await record(s, task_id, event_type, message, agent_id=agent_id, step_index=step_index, **metadata)


async def system_event(session: AsyncSession, component: str, event: str, message: str | None = None,
                       level: str = "INFO", **metadata: Any) -> None:
    session.add(SystemEvent(component=component, event=event, message=message, level=level, extra=redact(metadata)))
    await session.flush()


async def audit(session: AsyncSession, actor: str, action: str, target: str | None = None, **metadata: Any) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, extra=redact(metadata)))
    await session.flush()
