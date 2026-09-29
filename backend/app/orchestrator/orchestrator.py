"""The Orchestrator: the single execution pipeline for every task, whatever its source.

  receive -> normalize -> project/context -> retrieve memory -> plan (simple/complex) -> select agents
  -> execute steps (tools, approvals) -> validate (+retry) -> synthesize -> report -> consolidate memory
  -> session update -> final result

Each stage commits its own transaction, so progress survives restarts: completed steps are never
re-run, a paused/crashed step resumes from its checkpoint.
The orchestrator coordinates agents through the registry + runner interfaces; it contains no
agent-specific logic.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.agents.registry import AgentRegistry
from app.agents.runner import AgentRunner, StepOutcome
from app.core.config import get_settings
from app.core.enums import EventType, StepStatus, TaskStatus
from app.core.logging import task_id_var
from app.database.session import session_scope
from app.integrations.letta.client import LettaClient
from app.integrations.omniroute.client import ModelClient, get_model_client
from app.memory.consolidation import consolidate
from app.memory.context import MemoryItem, StepContext
from app.memory.service import MemoryService
from app.memory.session import SessionRuntime
from app.models import AgentDefinition, Lesson, Project, Task, TaskStep
from app.orchestrator.planner import Planner
from app.orchestrator.validator import validate_step_result, validate_task
from app.reports.service import generate_report
from app.services import journal
from app.tasks import service as task_service
from app.tools.registry import ToolRegistry, get_tool_registry

logger = logging.getLogger("atlas.orchestrator")
MAX_STEP_ATTEMPTS = 2
_GREETING = re.compile(r"^(hi|hello|hey|please|pls|atlas)[,!:\s]+", re.I)


def normalize_request(text: str) -> str:
    t = " ".join(text.split())
    t = _GREETING.sub("", t).strip()
    return t[0].upper() + t[1:] if t else t


class TaskPaused(Exception):
    pass


class Orchestrator:
    def __init__(self, model: ModelClient | None = None, tools: ToolRegistry | None = None,
                 letta: LettaClient | None = None, worker_id: str = "inline"):
        self.model = model or get_model_client()
        self.tools = tools or get_tool_registry()
        self.letta = letta or LettaClient()
        self.worker_id = worker_id
        self.settings = get_settings()

    # ============================================================================ entry point
    async def run(self, task_id: str) -> str:
        token = task_id_var.set(task_id)
        try:
            if not await self._prepare(task_id):
                return await self._status(task_id)
            while True:
                async with session_scope() as s:
                    task = await s.get(Task, task_id)
                    if task is None or task.status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                        return task.status if task else "MISSING"
                    step = next((st for st in task.steps if st.status != StepStatus.COMPLETED), None)
                if step is None:
                    break
                outcome = await self._run_step(task_id, step.step_index)
                if outcome in ("PAUSED", "RETRY", "FAILED", "CANCELLED"):
                    return await self._status(task_id)
            await self._finalize(task_id)
            return await self._status(task_id)
        finally:
            task_id_var.reset(token)

    async def _status(self, task_id: str) -> str:
        async with session_scope() as s:
            t = await s.get(Task, task_id)
            return t.status if t else "MISSING"

    # ============================================================================ stage 1: prepare
    async def _prepare(self, task_id: str) -> bool:
        async with session_scope() as s:
            task = await s.get(Task, task_id)
            if task is None or TaskStatus(task.status).is_terminal:
                return False
            resumed = bool(task.steps)
            sess = dict(task.session or {})
            if not task.normalized_request:
                task.normalized_request = normalize_request(task.original_request)
                project = await s.get(Project, task.project_id) if task.project_id else None
                sess["project"] = project.slug if project else None
                await journal.record(s, task.id, EventType.TASK_NORMALIZED, task.normalized_request[:300],
                                     project=sess["project"])
            if "memory_context" not in sess:
                mem = MemoryService(s)
                hits = await mem.search(task.normalized_request, project_id=task.project_id,
                                        limit=self.settings.memory_top_k)
                sess["memory_context"] = [
                    {"id": str(h.memory.id), "type": h.memory.type, "title": h.memory.title,
                     "content": h.memory.content[:1200], "score": h.score, "tags": h.memory.tags,
                     "source": h.memory.source} for h in hits]
                await journal.record(s, task.id, EventType.MEMORY_RETRIEVED,
                                     f"Retrieved {len(hits)} relevant memories",
                                     memories=[{"id": str(h.memory.id), "type": h.memory.type, "score": h.score,
                                                "similarity": h.similarity} for h in hits])
            if task.session_key and "conversation" not in sess:
                state = await SessionRuntime(s, self.letta).load(task.session_key)
                sess["conversation"] = state.conversation_text()
                sess["session_runtime"] = state.runtime
                if state.warning:
                    sess["session_warning"] = state.warning
            memories = [MemoryItem(**{k: m[k] for k in ("id", "type", "title", "content", "score", "tags",
                                                        "source")}) for m in sess["memory_context"]]
            if not resumed:
                task.status = TaskStatus.PLANNING
                await journal.record(s, task.id, EventType.PLANNING_STARTED, "Planning")
                planner = Planner(AgentRegistry(s), self.model)
                plan, meta = await planner.plan(task.normalized_request, memories)
                task.plan = {**plan.to_dict(), "planner_meta": meta}
                for i, st in enumerate(plan.steps):
                    s.add(TaskStep(task_id=task.id, step_index=i, agent_id=st.agent_id, objective=st.objective,
                                   depends_on=st.depends_on, status=StepStatus.PENDING))
                task.selected_agent = plan.steps[0].agent_id if len(plan.steps) == 1 else "multi-agent"
                await journal.record(s, task.id, EventType.PLAN_CREATED,
                                     f"{plan.complexity} plan with {len(plan.steps)} step(s) ({plan.planner})",
                                     complexity=plan.complexity, planner=plan.planner, rationale=plan.rationale,
                                     steps=[{"agent_id": x.agent_id, "objective": x.objective[:200]}
                                            for x in plan.steps], model=meta.get("model"))
                for i, st in enumerate(plan.steps):
                    await journal.record(s, task.id, EventType.AGENT_SELECTED, f"Step {i}: {st.agent_id}",
                                         agent_id=st.agent_id, step_index=i)
                if plan.guidance:
                    sess["guidance"] = plan.guidance
                if task.session_key:
                    await SessionRuntime(s, self.letta).set_active_task(task.session_key, task.id,
                                                                        task.original_request, task.plan)
            task.session = sess
            # planning may have taken a while (LLM call): re-check the status under a row lock so a cancel
            # issued in the meantime is never overwritten
            await s.refresh(task, attribute_names=["status"], with_for_update={"key_share": True})
            if task.status == TaskStatus.CANCELLED:
                return False
            task.status = TaskStatus.RUNNING
            await task_service.heartbeat(s, task.id, self.worker_id, self.settings.task_lease_seconds)
            return True

    # ============================================================================ stage 2: steps
    def _step_context(self, task: Task, step: TaskStep, agent: AgentDefinition) -> StepContext:
        sess = task.session or {}
        policy = agent.memory_policy or {}
        allowed = set(policy.get("retrieve_types") or ["SEMANTIC", "EPISODIC", "PROCEDURAL", "EVIDENCE", "CORE"])
        allowed.add("PROCEDURAL")  # learned procedures always reach the agent
        memories = [MemoryItem(**{k: m[k] for k in ("id", "type", "title", "content", "score", "tags", "source")})
                    for m in sess.get("memory_context", []) if m["type"] in allowed][: policy.get("top_k", 6)]
        prior = [r for r in sess.get("step_results", []) if r.get("step_index") in (step.depends_on or [])]
        return StepContext(task_id=task.id, request=task.normalized_request or task.original_request,
                           objective=step.objective, step_index=step.step_index, memories=memories,
                           prior_results=prior, conversation=sess.get("conversation", ""),
                           guidance=sess.get("guidance", []), project=sess.get("project"))

    async def _run_step(self, task_id: str, step_index: int) -> str:
        async with session_scope() as s:
            task = await task_service.lock_task(s, task_id)
            step = next(st for st in task.steps if st.step_index == step_index)
            if task.status == TaskStatus.CANCELLED:
                return "CANCELLED"
            registry = AgentRegistry(s)
            agent = await registry.get(step.agent_id)
            step.status = StepStatus.RUNNING
            step.attempts += 1
            step.started_at = step.started_at or datetime.now(UTC)
            task.current_step = step_index
            await journal.record(s, task.id, EventType.STEP_STARTED, step.objective[:300], agent_id=agent.id,
                                 step_index=step_index, attempt=step.attempts)
            await task_service.heartbeat(s, task.id, self.worker_id, self.settings.task_lease_seconds)

        # the agent loop runs in its own transaction; its checkpoint + tool records commit together
        try:
            async with session_scope() as s:
                task = await s.get(Task, task_id)
                assert task is not None
                step = next(st for st in task.steps if st.step_index == step_index)
                agent = await AgentRegistry(s).get(step.agent_id)
                ctx = self._step_context(task, step, agent)
                outcome: StepOutcome = await AgentRunner(s, self.model, self.tools).run(step, agent, ctx)
                return await self._apply_outcome(s, task, step, outcome)
        except Exception as exc:  # noqa: BLE001 - step failures are handled by the retry policy
            logger.exception("step_failed")
            async with session_scope() as s:
                task = await task_service.lock_task(s, task_id)
                step = next(st for st in task.steps if st.step_index == step_index)
                step.error = f"{type(exc).__name__}: {exc}"[:2000]
                await task_service.mark_interrupted(s, task.id, f"step failed: {step.error[:200]}")
                await journal.record(s, task.id, EventType.STEP_FAILED, step.error, agent_id=step.agent_id,
                                     step_index=step_index, attempt=step.attempts)
                if task.status == TaskStatus.CANCELLED:
                    step.status = StepStatus.FAILED
                    return "CANCELLED"
                if step.attempts < MAX_STEP_ATTEMPTS and task.retry_count < task.max_retries:
                    step.status = StepStatus.PENDING
                    task.retry_count += 1
                    task.status = TaskStatus.RETRYING
                    await task_service.release(s, task)
                    await journal.record(s, task.id, EventType.RETRY, f"Step {step_index} will be retried",
                                         retry_count=task.retry_count)
                    return "RETRY"
                step.status = StepStatus.FAILED
                await self._fail(s, task, f"Step {step_index} ({step.agent_id}) failed: {step.error}")
                return "FAILED"

    async def _apply_outcome(self, s, task: Task, step: TaskStep, outcome: StepOutcome) -> str:  # type: ignore[no-untyped-def]
        # the agent loop may have run for minutes: take the row lock and re-read the status so that a
        # cancel issued meanwhile wins (the work already done stays recorded in the journal)
        await s.refresh(task, attribute_names=["status"], with_for_update={"key_share": True})
        cancelled = task.status == TaskStatus.CANCELLED
        if outcome.status == "WAITING_APPROVAL":
            if cancelled:
                step.status = StepStatus.PENDING
                await task_service.reject_pending_approvals(s, task.id, "task cancelled")
                return "CANCELLED"
            step.status = StepStatus.WAITING_APPROVAL
            task.status = TaskStatus.WAITING_APPROVAL
            await task_service.release(s, task)
            return "PAUSED"
        validation = validate_step_result(outcome.result)
        await journal.record(s, task.id, EventType.VALIDATION, f"Step {step.step_index} validation: "
                             f"{'valid' if validation.valid else 'invalid'}", agent_id=step.agent_id,
                             step_index=step.step_index, **validation.to_dict())
        if not validation.valid and validation.retryable and step.attempts < MAX_STEP_ATTEMPTS:
            step.status = StepStatus.PENDING
            step.checkpoint = None
            await journal.record(s, task.id, EventType.RETRY, f"Re-running step {step.step_index} after failed "
                                 f"validation: {', '.join(validation.issues)}", step_index=step.step_index)
            return "CONTINUE"
        step.status = StepStatus.COMPLETED
        step.completed_at = datetime.now(UTC)
        step.result = {**outcome.result, "validation": validation.to_dict()}
        step.checkpoint = None
        sess = dict(task.session or {})
        results = [r for r in sess.get("step_results", []) if r.get("step_index") != step.step_index]
        results.append({"step_index": step.step_index, "agent_id": step.agent_id, "status": "COMPLETED",
                        **outcome.result, "validation": validation.to_dict()})
        sess["step_results"] = results
        task.session = sess
        await journal.record(s, task.id, EventType.STEP_COMPLETED, (outcome.result.get("summary") or "")[:300],
                             agent_id=step.agent_id, step_index=step.step_index)
        return "CANCELLED" if cancelled else "CONTINUE"

    async def _fail(self, s, task: Task, error: str) -> None:  # type: ignore[no-untyped-def]
        task.status = TaskStatus.FAILED
        task.error = error[:4000]
        task.completed_at = datetime.now(UTC)
        await task_service.release(s, task)
        await journal.record(s, task.id, EventType.TASK_FAILED, error[:500])

    # ============================================================================ stage 3: finalize
    async def _synthesize(self, task: Task, results: list[dict[str, Any]]) -> dict[str, Any]:
        if len(results) == 1:
            r = results[0]
            return {"summary": r.get("summary", ""), "findings": r.get("findings", []),
                    "recommendations": r.get("recommendations", []), "synthesis": "single-step"}
        offline = all(r.get("offline") for r in results)
        if not offline:
            data, resp = await self.model.chat_json([
                {"role": "system", "content": "Combine the validated results of several specialist agents into one "
                 "answer for the user. Do not add facts that are not in the results. Reply ONLY JSON: "
                 '{"summary": str, "findings": [str], "recommendations": [str]}'},
                {"role": "user", "content": str({"request": task.original_request,
                                                 "results": [{k: r.get(k) for k in ("agent_id", "summary", "findings",
                                                                                    "recommendations",
                                                                                    "configuration_required")}
                                                             for r in results]})[:20000]}], tier="reasoning")
            if isinstance(data, dict) and data.get("summary"):
                return {"summary": str(data["summary"]), "findings": data.get("findings") or [],
                        "recommendations": data.get("recommendations") or [], "synthesis": f"llm:{resp.model}"}
        parts = [f"[{r['agent_id']}] {r.get('summary', '')}" for r in results]
        return {"summary": "\n".join(parts),
                "findings": [f for r in results for f in r.get("findings", [])],
                "recommendations": list(dict.fromkeys(x for r in results for x in r.get("recommendations", []))),
                "synthesis": "concatenation (offline)"}

    async def _finalize(self, task_id: str) -> None:
        # phase 1 (no row lock held): task-level validation + synthesis (may call the LLM)
        async with session_scope() as s:
            task = await s.get(Task, task_id)
            assert task is not None
            results = sorted((task.session or {}).get("step_results", []), key=lambda r: r["step_index"])
            validation = validate_task(results)
            await journal.record(s, task.id, EventType.VALIDATION, "Task-level validation", **validation.to_dict())
            synth = await self._synthesize(task, results)
        # phase 2 (row lock): persist the result unless the task was cancelled in the meantime
        async with session_scope() as s:
            task = await task_service.lock_task(s, task_id)
            if task.status == TaskStatus.CANCELLED:
                return
            sess = dict(task.session or {})
            applied = [m for m in sess.get("memory_context", []) if m["type"] == "PROCEDURAL"]
            sess["applied_lesson_memory_ids"] = [m["id"] for m in applied]
            final = {
                **synth,
                "offline": all(r.get("offline") for r in results),
                "validation": validation.to_dict(),
                "configuration_required": sorted({c for r in results for c in r.get("configuration_required") or []}),
                "applied_lessons": [{"memory_id": m["id"], "content": m["content"]} for m in applied],
                "steps": [{k: r.get(k) for k in ("step_index", "agent_id", "status", "summary", "findings",
                                                 "actions_taken", "recommendations", "tools_used", "evidence",
                                                 "model", "offline", "confidence")} for r in results],
            }
            if applied:
                await s.execute(update(Lesson).where(Lesson.memory_id.in_([m["id"] for m in applied]))
                                .values(usage_count=Lesson.usage_count + 1))
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now(UTC)
            task.result = final
            task.session = sess
            plan = task.plan or {}
            if plan.get("needs_report") or len(results) > 1 or task.source == "scheduler":
                report = await generate_report(s, task, final)
                task.result = {**final, "report_id": str(report.id)}
            agents = {a.id: a for a in (await s.execute(select(AgentDefinition))).scalars()}
            cons = await consolidate(s, task, results, final, agents, MemoryService(s))
            task.result = {**task.result, "memory": {"stored": len(cons.stored), "reinforced": len(cons.updated),
                                                     "rejected": len(cons.rejected)}}
            if task.session_key:
                runtime = await SessionRuntime(s, self.letta).record_exchange(
                    task.session_key, task.id, task.original_request, final["summary"])
                task.result = {**task.result, "session_runtime": runtime}
            await task_service.release(s, task)
            await journal.record(s, task.id, EventType.TASK_COMPLETED, final["summary"][:500],
                                 offline=final["offline"], valid=validation.valid)
