"""Tool execution with security controls:

* every call (success, error, blocked, waiting approval, rejected) is recorded in tool_executions
* arguments are redacted before persistence; secrets never reach logs
* DESTRUCTIVE / approval-required tools pause the task in WAITING_APPROVAL until a human decides
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ApprovalStatus, EventType, RiskLevel, ToolExecutionStatus
from app.core.logging import redact
from app.database.session import session_scope
from app.models import Approval, ToolExecution
from app.schemas.common import NormalizedResult
from app.services import journal
from app.tasks.service import CANCEL_ACTOR
from app.tools.base import ToolProviderUnavailable, ToolSpec
from app.tools.normalizer import error_result, normalize
from app.tools.registry import ToolRegistry


class ApprovalRequired(Exception):
    def __init__(self, approval_id: uuid.UUID, tool_id: str, summary: str):
        super().__init__(summary)
        self.approval_id = approval_id
        self.tool_id = tool_id
        self.summary = summary


@dataclass
class ToolContext:
    task_id: str
    step_index: int | None
    agent_id: str | None


def args_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict,
               "array": list}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Lightweight JSON-schema check (required fields + primitive types)."""
    problems = []
    if not isinstance(arguments, dict):
        return ["arguments must be an object"]
    for req in schema.get("required", []) or []:
        if req not in arguments:
            problems.append(f"missing required argument '{req}'")
    for key, val in arguments.items():
        prop = (schema.get("properties") or {}).get(key)
        if not prop or "type" not in prop or val is None:
            continue
        types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
        py: list[type] = []
        for name in types:
            t = _JSON_TYPES.get(name, object)
            py.extend(t if isinstance(t, tuple) else (t,))
        if isinstance(val, bool) and bool not in py and object not in py:
            problems.append(f"argument '{key}' should be {'/'.join(types)}")
        elif not isinstance(val, tuple(py)):
            problems.append(f"argument '{key}' should be {'/'.join(types)}")
    return problems


def describe_action(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={json.dumps(v, default=str)[:80]}" for k, v in redact(arguments).items())
    return f"{spec.name}({shown})" + (f" via {spec.provider}" if spec.provider else "")


class ToolExecutor:
    def __init__(self, session: AsyncSession, registry: ToolRegistry):
        self.session = session
        self.registry = registry

    async def _record(self, ctx: ToolContext, spec_id: str, risk: str, arguments: dict[str, Any], status: str,
                      result: NormalizedResult | None, error: str | None, approval_id: uuid.UUID | None,
                      duration_ms: int | None, exec_id: uuid.UUID | None = None) -> ToolExecution:
        row = ToolExecution(id=exec_id or uuid.uuid4(), task_id=ctx.task_id, step_index=ctx.step_index, agent_id=ctx.agent_id, tool_id=spec_id,
                            arguments=redact(arguments), arguments_hash=args_hash(arguments), risk_level=risk,
                            status=status, result=result.model_dump() if result else None, error=error,
                            approval_id=approval_id, duration_ms=duration_ms)
        self.session.add(row)
        await self.session.flush()
        return row

    async def _approval_gate(self, ctx: ToolContext, spec: ToolSpec, arguments: dict[str, Any]) -> uuid.UUID | None:
        """Returns the approval id if approved; raises ApprovalRequired or returns a rejection marker."""
        h = args_hash(arguments)
        existing = (await self.session.execute(
            select(Approval).where(Approval.task_id == ctx.task_id, Approval.tool_id == spec.id,
                                   Approval.arguments_hash == h).order_by(Approval.requested_at.desc()).limit(1)
        )).scalar_one_or_none()
        # an approval auto-closed because the task was cancelled does not count as a human decision:
        # if the task is retried later, the human is asked again
        if existing is not None and not (existing.status == ApprovalStatus.REJECTED
                                         and existing.decided_by == CANCEL_ACTOR):
            if existing.status == ApprovalStatus.APPROVED:
                return existing.id
            if existing.status == ApprovalStatus.REJECTED:
                raise _Rejected(existing)
            raise ApprovalRequired(existing.id, spec.id, existing.action_summary)
        summary = describe_action(spec, arguments)
        appr = Approval(task_id=ctx.task_id, step_index=ctx.step_index, agent_id=ctx.agent_id, tool_id=spec.id,
                        action_summary=summary, risk_level=str(spec.risk_level), arguments=redact(arguments),
                        arguments_hash=h, status=ApprovalStatus.PENDING)
        self.session.add(appr)
        await self.session.flush()
        await self._record(ctx, spec.id, str(spec.risk_level), arguments, ToolExecutionStatus.WAITING_APPROVAL,
                           None, None, appr.id, None)
        await journal.record(self.session, ctx.task_id, EventType.APPROVAL_REQUESTED,
                             f"Approval required: {summary}", agent_id=ctx.agent_id, step_index=ctx.step_index,
                             approval_id=str(appr.id), tool_id=spec.id, risk_level=str(spec.risk_level))
        raise ApprovalRequired(appr.id, spec.id, summary)

    async def _prior_mutation(self, ctx: ToolContext, spec: ToolSpec, h: str) -> ToolExecution | None:
        """Latest earlier execution of the same mutating call in this task (committed rows only)."""
        return (await self.session.execute(
            select(ToolExecution).where(ToolExecution.task_id == ctx.task_id, ToolExecution.tool_id == spec.id,
                                        ToolExecution.arguments_hash == h,
                                        ToolExecution.status.in_([ToolExecutionStatus.SUCCESS,
                                                                  ToolExecutionStatus.RUNNING,
                                                                  ToolExecutionStatus.INTERRUPTED]))
            .order_by(ToolExecution.started_at.desc()).limit(1))).scalar_one_or_none()

    async def execute(self, ctx: ToolContext, spec: ToolSpec, arguments: dict[str, Any]) -> NormalizedResult:
        arguments = arguments or {}
        problems = validate_arguments(spec.input_schema, arguments)
        if problems:
            res = error_result(spec.id, "error", "Invalid arguments: " + "; ".join(problems))
            await self._record(ctx, spec.id, str(spec.risk_level), arguments, ToolExecutionStatus.BLOCKED, res,
                               res.summary, None, 0)
            await journal.record(self.session, ctx.task_id, EventType.TOOL_BLOCKED, res.summary,
                                 agent_id=ctx.agent_id, step_index=ctx.step_index, tool_id=spec.id,
                                 arguments=arguments)
            return res

        approval_id = None
        if spec.requires_approval or spec.risk_level == RiskLevel.DESTRUCTIVE:
            try:
                approval_id = await self._approval_gate(ctx, spec, arguments)
            except _Rejected as rej:
                res = error_result(spec.id, "rejected",
                                   f"Human rejected this action: {rej.approval.reason or 'no reason given'}")
                await self._record(ctx, spec.id, str(spec.risk_level), arguments, ToolExecutionStatus.REJECTED,
                                   res, res.summary, rej.approval.id, 0)
                await journal.record(self.session, ctx.task_id, EventType.TOOL_BLOCKED, res.summary,
                                     agent_id=ctx.agent_id, step_index=ctx.step_index, tool_id=spec.id)
                return res

        provider = self.registry.provider(spec.provider)
        if provider is None or not provider.configured:
            res = error_result(spec.id, "configuration_required",
                               f"CONFIGURATION REQUIRED: tool provider '{spec.provider}' is not configured")
            await self._record(ctx, spec.id, str(spec.risk_level), arguments,
                               ToolExecutionStatus.CONFIGURATION_REQUIRED, res, res.summary, approval_id, 0)
            await journal.record(self.session, ctx.task_id, EventType.TOOL_RESULT, f"{spec.id}: {res.summary}",
                                 agent_id=ctx.agent_id, step_index=ctx.step_index, tool_id=spec.id,
                                 status=str(ToolExecutionStatus.CONFIGURATION_REQUIRED))
            return res

        h = args_hash(arguments)
        if spec.risk_level != RiskLevel.READ:
            # exactly-once side effects across crashes/retries: a completed mutating call is replayed from
            # its recorded result; an interrupted one (outcome unknown) is never re-run automatically
            prior = await self._prior_mutation(ctx, spec, h)
            if prior is not None and prior.status == ToolExecutionStatus.SUCCESS and prior.result:
                res = NormalizedResult(**{**prior.result, "metadata": {**prior.result.get("metadata", {}),
                                                                        "replayed_from": str(prior.id)}})
                await journal.record(self.session, ctx.task_id, EventType.TOOL_RESULT,
                                     f"{spec.id}: replayed recorded result (not re-executed)",
                                     agent_id=ctx.agent_id, step_index=ctx.step_index, tool_id=spec.id,
                                     status="REPLAYED", tool_execution_id=str(prior.id))
                return res
            if prior is not None:
                msg = (f"A previous attempt of {spec.name} was interrupted (outcome unknown). It is not re-executed "
                       f"automatically: verify the current state, then retry the task.")
                res = error_result(spec.id, "error", msg)
                await journal.record(self.session, ctx.task_id, EventType.TOOL_BLOCKED, msg, agent_id=ctx.agent_id,
                                     step_index=ctx.step_index, tool_id=spec.id, tool_execution_id=str(prior.id))
                return res

        # write-ahead: the execution record + TOOL_CALLED are committed BEFORE the provider is called, so a
        # crash can never hide that a tool was (possibly) executed, and progress is visible live
        exec_id = uuid.uuid4()
        async with session_scope() as ws:
            ws.add(ToolExecution(id=exec_id, task_id=ctx.task_id, step_index=ctx.step_index, agent_id=ctx.agent_id,
                                 tool_id=spec.id, arguments=redact(arguments), arguments_hash=h,
                                 risk_level=str(spec.risk_level), status=ToolExecutionStatus.RUNNING,
                                 approval_id=approval_id))
            await journal.record(ws, ctx.task_id, EventType.TOOL_CALLED, f"Calling {spec.id}", agent_id=ctx.agent_id,
                                 step_index=ctx.step_index, tool_id=spec.id, risk_level=str(spec.risk_level),
                                 arguments=arguments, tool_execution_id=str(exec_id))
        started = time.monotonic()
        try:
            raw = await provider.call_tool(spec, arguments)
            res = normalize(spec, raw, task_id=ctx.task_id, execution_id=str(exec_id))
            status = ToolExecutionStatus.ERROR if res.status == "error" else ToolExecutionStatus.SUCCESS
        except ToolProviderUnavailable as exc:
            res = error_result(spec.id, "error", str(exc))
            status = ToolExecutionStatus.ERROR
        duration = int((time.monotonic() - started) * 1000)
        async with session_scope() as ws:
            row = await ws.get(ToolExecution, exec_id)
            assert row is not None
            row.status, row.result, row.duration_ms = str(status), res.model_dump(), duration
            row.error = "; ".join(res.errors) or None
            await journal.record(ws, ctx.task_id, EventType.TOOL_RESULT, f"{spec.id}: {res.summary[:200]}",
                                 agent_id=ctx.agent_id, step_index=ctx.step_index, tool_id=spec.id,
                                 status=str(status), duration_ms=duration, tool_execution_id=str(exec_id))
        return res


class _Rejected(Exception):
    def __init__(self, approval: Approval):
        super().__init__("rejected")
        self.approval = approval
