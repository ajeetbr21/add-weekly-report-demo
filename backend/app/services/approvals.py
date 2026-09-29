"""Human approval workflow shared by Web UI and Telegram."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ApprovalStatus, EventType, StepStatus, TaskStatus
from app.core.errors import ConflictError, NotFoundError
from app.models import Approval, Task
from app.schemas.api import ApprovalDecision
from app.services import journal


async def list_approvals(session: AsyncSession, status: str | None = None, task_id: str | None = None,
                         limit: int = 50, offset: int = 0) -> tuple[list[Approval], int]:
    stmt = select(Approval).order_by(Approval.requested_at.desc())
    cstmt = select(func.count()).select_from(Approval)
    if status:
        stmt, cstmt = stmt.where(Approval.status == status), cstmt.where(Approval.status == status)
    if task_id:
        stmt, cstmt = stmt.where(Approval.task_id == task_id), cstmt.where(Approval.task_id == task_id)
    return (list((await session.execute(stmt.limit(limit).offset(offset))).scalars()),
            (await session.execute(cstmt)).scalar_one())


async def decide(session: AsyncSession, approval_id: uuid.UUID, decision: ApprovalDecision) -> Approval:
    appr = (await session.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
            ).scalar_one_or_none()
    if appr is None:
        raise NotFoundError("Approval not found")
    if appr.status != ApprovalStatus.PENDING:
        raise ConflictError(f"Approval already {appr.status}")
    appr.status = ApprovalStatus.APPROVED if decision.approve else ApprovalStatus.REJECTED
    appr.decided_at = datetime.now(UTC)
    appr.decided_by = decision.decided_by
    appr.decision_channel = decision.channel
    appr.reason = decision.reason
    await journal.record(session, appr.task_id, EventType.APPROVAL_DECIDED,
                         f"{appr.status} by {decision.decided_by} via {decision.channel}: {appr.action_summary}",
                         agent_id=appr.agent_id, step_index=appr.step_index, approval_id=str(appr.id),
                         status=str(appr.status))
    await journal.audit(session, f"{decision.channel}:{decision.decided_by}", f"approval.{appr.status.lower()}",
                        str(appr.id), task_id=appr.task_id, tool_id=appr.tool_id, reason=decision.reason)

    task = await session.get(Task, appr.task_id, with_for_update={"key_share": True}, populate_existing=True)
    if task is not None and task.status == TaskStatus.WAITING_APPROVAL:
        pending = (await session.execute(select(func.count()).select_from(Approval).where(
            Approval.task_id == task.id, Approval.status == ApprovalStatus.PENDING))).scalar_one()
        if pending == 0:
            for step in task.steps:
                if step.status == StepStatus.WAITING_APPROVAL:
                    step.status = StepStatus.PENDING
            task.status = TaskStatus.PENDING  # re-queued; the step resumes from its checkpoint
    await session.flush()
    return appr
