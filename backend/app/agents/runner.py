"""Agent execution: the tool-calling loop shared by every agent.

LLM mode:   messages -> OmniRoute (tier from agent.model_preference) -> tool calls -> ToolExecutor ->
            normalized results back to the model -> ... -> final JSON answer.
Offline:    heuristics.select_tool_calls -> ToolExecutor -> heuristics.compose_offline_result.

The loop checkpoints its message history into task_steps.checkpoint after every tool round, so a step
paused for approval (or interrupted by a crash) resumes exactly where it stopped.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.heuristics import compose_offline_result, select_tool_calls
from app.core.config import get_settings
from app.core.enums import EventType
from app.core.logging import agent_id_var
from app.database.session import session_scope
from app.integrations.omniroute.client import ModelClient, ModelResponse, parse_json_loose
from app.memory.context import StepContext, build_messages, compact_messages
from app.models import AgentDefinition, AgentRun, TaskStep
from app.services import journal
from app.tools.base import ToolSpec
from app.tools.executor import ApprovalRequired, ToolContext, ToolExecutor
from app.tools.registry import ToolRegistry

logger = logging.getLogger("atlas.agent")
MAX_TOOL_RESULT_CHARS = 6000


@dataclass
class StepOutcome:
    status: str  # COMPLETED | WAITING_APPROVAL | FAILED
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None


def _pending_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls of the last assistant message that have no tool response yet."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in messages[i + 1:] if x.get("role") == "tool"}
            return [tc for tc in m["tool_calls"] if tc["id"] not in answered]
        if m.get("role") == "assistant":
            return []
    return []


class AgentRunner:
    def __init__(self, session: AsyncSession, model: ModelClient, registry: ToolRegistry):
        self.session = session
        self.model = model
        self.registry = registry
        self.executor = ToolExecutor(session, registry)
        self.settings = get_settings()

    def _unavailable_integrations(self, agent: AgentDefinition, tools: list[ToolSpec]) -> list[str]:
        missing = []
        providers = {p.split(":", 1)[0] for p in agent.tools or [] if ":" in p}
        for prov in sorted(providers):
            provider = self.registry.provider(prov)
            if provider is None or not provider.configured:
                if prov == "composio":
                    kits = sorted({p.split(":", 1)[1].split("_")[0].rstrip("*").lower() or "all"
                                   for p in agent.tools if p.startswith("composio:")})
                    missing.append(f"Composio MCP ({', '.join(kits)} toolkits) is not configured - set "
                                   f"COMPOSIO_ENABLED, COMPOSIO_MCP_URL and COMPOSIO_API_KEY")
                else:
                    missing.append(f"tool provider '{prov}' is not configured")
            elif not any(t.provider == prov for t in tools):
                status = self.registry.provider_status.get(prov, {})
                if status.get("status") not in (None, "OK"):
                    missing.append(f"tool provider '{prov}' is {status.get('status')}: {status.get('detail', '')}")
        return missing

    async def run(self, step: TaskStep, agent: AgentDefinition, ctx: StepContext) -> StepOutcome:
        token = agent_id_var.set(agent.id)
        # write-ahead: the run row + AGENT_STARTED are committed immediately, so an interrupted attempt
        # stays visible in the history (recovery marks it INTERRUPTED)
        run_id = uuid.uuid4()
        async with session_scope() as ws:
            ws.add(AgentRun(id=run_id, task_id=ctx.task_id, step_index=step.step_index, agent_id=agent.id,
                            status="RUNNING", runtime="atlas-agent-loop"))
            await journal.record(ws, ctx.task_id, EventType.AGENT_STARTED, f"{agent.name} started",
                                 agent_id=agent.id, step_index=step.step_index, agent_run_id=str(run_id),
                                 resumed=bool(step.checkpoint))
        run = await self.session.get(AgentRun, run_id)
        assert run is not None
        try:
            all_tools = await self.registry.all_tools(self.session)
            tools = ToolRegistry.for_agent(agent.tools or [], all_tools)
            unavailable = self._unavailable_integrations(agent, tools)
            checkpoint = step.checkpoint or {}
            if checkpoint.get("mode") == "offline":
                outcome = await self._run_offline(step, agent, ctx, tools, unavailable, run,
                                                  checkpoint.get("reason"))
            else:
                outcome = await self._run_llm(step, agent, ctx, tools, unavailable, run)
            run.status = outcome.status
            run.output_summary = (outcome.result.get("summary") or "")[:2000] if outcome.result else None
            run.error = outcome.error
            if outcome.status != "WAITING_APPROVAL":
                run.completed_at = datetime.now(UTC)
            await journal.record(self.session, ctx.task_id, EventType.AGENT_COMPLETED,
                                 f"{agent.name} finished with {outcome.status}", agent_id=agent.id,
                                 step_index=step.step_index, status=outcome.status, iterations=run.iterations)
            return outcome
        except Exception as exc:
            run.status, run.error, run.completed_at = "FAILED", str(exc)[:2000], datetime.now(UTC)
            raise
        finally:
            agent_id_var.reset(token)

    # ------------------------------------------------------------------------------------ LLM path
    async def _run_llm(self, step: TaskStep, agent: AgentDefinition, ctx: StepContext, tools: list[ToolSpec],
                       unavailable: list[str], run: AgentRun) -> StepOutcome:
        by_fn = {t.function_name: t for t in tools}
        checkpoint = step.checkpoint or {}
        messages: list[dict[str, Any]] = checkpoint.get("messages") or build_messages(
            agent.instructions + (("\n\nUnavailable integrations: " + "; ".join(unavailable)) if unavailable else ""),
            ctx, [t.id for t in tools])
        iteration = int(checkpoint.get("iteration", 0))
        tool_log: list[dict[str, Any]] = list(checkpoint.get("tool_log", []))
        tier = agent.model_preference or "default"

        while True:
            # 1) finish any tool calls that are still pending (resume after approval / crash)
            pending = _pending_tool_calls(messages)
            for tc in pending:
                spec = by_fn.get(tc["function"]["name"])
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if spec is None:
                    content = json.dumps({"status": "error", "errors": [f"Unknown tool {tc['function']['name']}"]})
                else:
                    try:
                        res = await self.executor.execute(ToolContext(ctx.task_id, step.step_index, agent.id),
                                                          spec, args)
                    except ApprovalRequired as appr:
                        step.checkpoint = {"mode": "llm", "messages": messages, "iteration": iteration,
                                           "tool_log": tool_log}
                        return StepOutcome(status="WAITING_APPROVAL", approval_id=str(appr.approval_id),
                                           result={"summary": f"Waiting for approval: {appr.summary}"})
                    tool_log.append({"tool_id": spec.id, "arguments": args, "result": res.model_dump()})
                    content = json.dumps(res.model_dump(), default=str)[:MAX_TOOL_RESULT_CHARS]
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
            if pending:
                messages, compacted = compact_messages(messages)
                if compacted:
                    await journal.record(self.session, ctx.task_id, EventType.CONTEXT_COMPACTED,
                                         "Older tool outputs compacted", agent_id=agent.id, step_index=step.step_index)
                step.checkpoint = {"mode": "llm", "messages": messages, "iteration": iteration, "tool_log": tool_log}
                await self.session.flush()

            if iteration >= self.settings.agent_max_iterations:
                messages.append({"role": "user", "content": "Iteration limit reached. Reply now with the final "
                                                            "JSON object using what you have."})
            # 2) ask the model
            resp = await self.model.chat(messages, tier=tier,
                                         tools=[t.to_openai_tool() for t in tools]
                                         if iteration < self.settings.agent_max_iterations else None)
            iteration += 1
            run.iterations = iteration
            await self._record_model_call(ctx, agent, step, run, resp)
            if resp.offline:
                if iteration == 1 and not tool_log:
                    step.checkpoint = {"mode": "offline", "reason": resp.fallback_reason}
                    return await self._run_offline(step, agent, ctx, tools, unavailable, run, resp.fallback_reason)
                result = compose_offline_result(agent.name, ctx, tool_log, unavailable, resp.fallback_reason)
                return self._finish(result, tool_log, resp, offline=True)
            if resp.tool_calls and iteration <= self.settings.agent_max_iterations:
                messages.append(resp.assistant_message())
                step.checkpoint = {"mode": "llm", "messages": messages, "iteration": iteration, "tool_log": tool_log}
                await self.session.flush()
                continue
            return self._finish(self._parse_final(resp.content or ""), tool_log, resp, offline=False,
                                unavailable=unavailable)

    async def _record_model_call(self, ctx: StepContext, agent: AgentDefinition, step: TaskStep, run: AgentRun,
                                 resp: ModelResponse) -> None:
        run.model, run.model_provider = resp.model, resp.provider
        run.prompt_tokens += resp.usage.get("prompt_tokens", 0)
        run.completion_tokens += resp.usage.get("completion_tokens", 0)
        await journal.record_now(ctx.task_id, EventType.MODEL_CALL,
                             "Offline fallback (no LLM)" if resp.offline else f"Model call via OmniRoute: {resp.model}",
                             agent_id=agent.id, step_index=step.step_index, model=resp.model, provider=resp.provider,
                             offline=resp.offline, fallback_reason=resp.fallback_reason, usage=resp.usage,
                             tool_calls=[tc.name for tc in resp.tool_calls], duration_ms=resp.duration_ms)

    @staticmethod
    def _parse_final(content: str) -> dict[str, Any]:
        try:
            data = parse_json_loose(content)
            if isinstance(data, dict) and data.get("summary"):
                return data
        except (ValueError, json.JSONDecodeError):
            pass
        return {"summary": content.strip()[:4000] or "(empty answer)", "findings": [], "recommendations": [],
                "facts": [], "confidence": 0.5}

    def _finish(self, result: dict[str, Any], tool_log: list[dict[str, Any]], resp: ModelResponse, *,
                offline: bool, unavailable: list[str] | None = None) -> StepOutcome:
        evidence = [ev for t in tool_log for ev in (t["result"].get("evidence") or [])]
        result = {
            "summary": str(result.get("summary", ""))[:6000],
            "findings": [str(x) for x in result.get("findings") or []][:30],
            "actions_taken": [str(x) for x in result.get("actions_taken") or []][:30],
            "recommendations": [str(x) for x in result.get("recommendations") or []][:30],
            "facts": [str(x) for x in result.get("facts") or []][:10],
            "confidence": float(result.get("confidence") or 0.5),
            "configuration_required": result.get("configuration_required")
            or [f"CONFIGURATION REQUIRED: {u}" for u in (unavailable or [])],
            "tools_used": [{"tool_id": t["tool_id"], "status": t["result"].get("status"),
                            "summary": (t["result"].get("summary") or "")[:300]} for t in tool_log],
            "evidence": evidence[:30],
            "model": resp.model, "model_provider": resp.provider, "offline": offline,
        }
        return StepOutcome(status="COMPLETED", result=result)

    # ------------------------------------------------------------------------------------ offline path
    async def _run_offline(self, step: TaskStep, agent: AgentDefinition, ctx: StepContext, tools: list[ToolSpec],
                           unavailable: list[str], run: AgentRun, reason: str | None) -> StepOutcome:
        checkpoint = step.checkpoint or {}
        done: list[dict[str, Any]] = list(checkpoint.get("tool_log", []))
        done_keys = {(d["tool_id"], json.dumps(d["arguments"], sort_keys=True)) for d in done}
        for call in select_tool_calls(ctx, tools):
            key = (call.spec.id, json.dumps(call.arguments, sort_keys=True))
            if key in done_keys:
                continue
            try:
                res = await self.executor.execute(ToolContext(ctx.task_id, step.step_index, agent.id), call.spec,
                                                  call.arguments)
            except ApprovalRequired as appr:
                step.checkpoint = {"mode": "offline", "reason": reason, "tool_log": done}
                return StepOutcome(status="WAITING_APPROVAL", approval_id=str(appr.approval_id),
                                   result={"summary": f"Waiting for approval: {appr.summary}"})
            done.append({"tool_id": call.spec.id, "arguments": call.arguments, "result": res.model_dump()})
            step.checkpoint = {"mode": "offline", "reason": reason, "tool_log": done}
            await self.session.flush()
        run.iterations = max(run.iterations, 1)
        run.model, run.model_provider = "offline-heuristic", "offline"
        result = compose_offline_result(agent.name, ctx, done, unavailable, reason)
        return self._finish(result, done, ModelResponse(content=None, model="offline-heuristic", provider="offline",
                                                        offline=True), offline=True)
