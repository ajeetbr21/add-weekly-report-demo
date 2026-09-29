"""Context window management.

The model never sees the full task history. Context = agent instructions + current task/objective +
current session (recent conversation) + relevant retrieved memory + relevant prior step/tool results,
all under a character budget. Old tool outputs in a running agent loop are compacted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings


@dataclass
class MemoryItem:
    id: str
    type: str
    title: str | None
    content: str
    score: float
    tags: list[str] = field(default_factory=list)
    source: str = ""

    def render(self, limit: int = 600) -> str:
        head = f"[{self.type}{' ' + self.title if self.title else ''}]"
        return f"- {head} {self.content[:limit]}"


@dataclass
class StepContext:
    task_id: str
    request: str
    objective: str
    step_index: int
    project: str | None = None
    memories: list[MemoryItem] = field(default_factory=list)
    prior_results: list[dict[str, Any]] = field(default_factory=list)
    conversation: str = ""
    guidance: list[str] = field(default_factory=list)

    @property
    def lessons(self) -> list[MemoryItem]:
        return [m for m in self.memories if m.type == "PROCEDURAL"]


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 20] + " …[truncated]"


def build_messages(instructions: str, ctx: StepContext, tool_names: list[str]) -> list[dict[str, Any]]:
    budget = get_settings().context_max_chars
    mem_block = "\n".join(m.render() for m in ctx.memories) or "(none)"
    guidance = "\n".join(f"- {g}" for g in ctx.guidance) or "(none)"
    system = (f"{instructions}\n\nProject: {ctx.project or 'default'}\n"
              f"Available tools: {', '.join(tool_names) if tool_names else 'none (answer from knowledge/memory)'}\n\n"
              f"Relevant memory (retrieved for this task; lessons/procedures are learned from past feedback):\n"
              f"{_clip(mem_block, budget // 3)}\n\nPlanning guidance:\n{_clip(guidance, 2000)}")
    prior = ""
    if ctx.prior_results:
        parts = [f"Step {r.get('step_index')} ({r.get('agent_id')}): {r.get('summary', '')}" for r in ctx.prior_results]
        prior = "\n\nResults from earlier steps of this task:\n" + _clip("\n".join(parts), budget // 4)
    convo = f"\n\nRecent conversation in this session:\n{_clip(ctx.conversation, 2500)}" if ctx.conversation else ""
    user = (f"Task {ctx.task_id}. Original request:\n{_clip(ctx.request, 4000)}\n\n"
            f"Your objective for this step:\n{ctx.objective}{prior}{convo}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def compact_messages(messages: list[dict[str, Any]], max_chars: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Shrink older tool outputs when the running conversation exceeds the budget."""
    max_chars = max_chars or get_settings().context_max_chars
    size = sum(len(json.dumps(m, default=str)) for m in messages)
    if size <= max_chars:
        return messages, False
    out = [dict(m) for m in messages]
    tool_idx = [i for i, m in enumerate(out) if m.get("role") == "tool"]
    for i in tool_idx[:-2]:  # keep the two latest tool outputs intact
        content = str(out[i].get("content", ""))
        if len(content) > 600:
            out[i]["content"] = content[:600] + " …[compacted: older tool output truncated]"
    return out, True
