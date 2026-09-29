"""Session memory: temporary, per-conversation / per-task working state.

* Conversation-level session state (recent exchanges, active task) lives in the SessionRuntime:
  Letta memory blocks when LETTA_ENABLED=true, otherwise the local `sessions` table.
* Task-level working state (plan, intermediate step results, tool outputs, variables) lives in
  `tasks.session` (JSONB) so it survives restarts.

Nothing here is promoted to long-term memory automatically; only the consolidation pipeline
(app.memory.consolidation) may create long-term memories.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.letta.client import LettaClient, LettaError
from app.models import Session

logger = logging.getLogger("atlas.session")

MAX_EXCHANGES = 8
BLOCK_LABELS = ("conversation", "active_task", "scratchpad")


@dataclass
class SessionState:
    key: str
    runtime: str
    conversation: list[dict[str, str]] = field(default_factory=list)
    active_task: dict[str, Any] = field(default_factory=dict)
    scratchpad: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None

    def conversation_text(self, max_chars: int = 3000) -> str:
        lines = [f"User: {x.get('request', '')}\nAtlas: {x.get('response', '')}" for x in self.conversation]
        text = "\n".join(lines)
        return text[-max_chars:]


class SessionRuntime:
    """Letta-backed session runtime with transparent fallback to the local sessions table."""

    def __init__(self, session: AsyncSession, letta: LettaClient | None = None):
        self.db = session
        self.letta = letta or LettaClient()

    async def _row(self, key: str) -> Session:
        row = await self.db.get(Session, key)
        if row is None:
            row = Session(key=key, runtime="local", state={})
            self.db.add(row)
            await self.db.flush()
        return row

    async def load(self, key: str) -> SessionState:
        row = await self._row(key)
        if self.letta.enabled:
            try:
                blocks = await self._ensure_letta_blocks(row)
                values = {}
                for label, block_id in blocks.items():
                    raw = (await self.letta.get_block(block_id)).get("value") or ""
                    values[label] = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
                return SessionState(key=key, runtime="letta", conversation=values.get("conversation") or [],
                                    active_task=values.get("active_task") or {},
                                    scratchpad=values.get("scratchpad") or {})
            except (LettaError, json.JSONDecodeError, KeyError) as exc:
                logger.warning(f"letta_session_load_failed: {exc}")
                state = self._local_state(row)
                state.warning = f"Letta unavailable, used local session store: {str(exc)[:160]}"
                return state
        return self._local_state(row)

    def _local_state(self, row: Session) -> SessionState:
        st = row.state or {}
        return SessionState(key=row.key, runtime="local", conversation=st.get("conversation") or [],
                            active_task=st.get("active_task") or {}, scratchpad=st.get("scratchpad") or {})

    async def _ensure_letta_blocks(self, row: Session) -> dict[str, str]:
        blocks: dict[str, str] = dict((row.state or {}).get("letta_blocks") or {})
        changed = False
        for label in BLOCK_LABELS:
            if label not in blocks:
                blocks[label] = await self.letta.create_block(
                    label=label, value="[]" if label == "conversation" else "{}",
                    description=f"Atlas session {label} for {row.key}")
                changed = True
        if changed:
            row.state = {**(row.state or {}), "letta_blocks": blocks}
            row.runtime = "letta"
            await self.db.flush()
        return blocks

    async def save(self, state: SessionState) -> str:
        """Persist session state. Returns the runtime actually used."""
        state.conversation = state.conversation[-MAX_EXCHANGES:]
        row = await self._row(state.key)
        payload = {"conversation": state.conversation, "active_task": state.active_task,
                   "scratchpad": state.scratchpad}
        if self.letta.enabled:
            try:
                blocks = await self._ensure_letta_blocks(row)
                for label in BLOCK_LABELS:
                    await self.letta.update_block(blocks[label], json.dumps(payload[label], default=str)[:7900])
                # keep a local mirror too (cheap, and useful if Letta is later disabled)
                row.state = {**(row.state or {}), **payload}
                await self.db.flush()
                return "letta"
            except LettaError as exc:
                logger.warning(f"letta_session_save_failed: {exc}")
        row.state = {**(row.state or {}), **payload}
        row.runtime = row.runtime or "local"
        await self.db.flush()
        return "local"

    async def record_exchange(self, key: str, task_id: str, request: str, response: str) -> str:
        state = await self.load(key)
        state.conversation.append({"task_id": task_id, "request": request[:1000], "response": response[:1500]})
        state.active_task = {}
        return await self.save(state)

    async def set_active_task(self, key: str, task_id: str, request: str, plan: dict[str, Any] | None) -> str:
        state = await self.load(key)
        state.active_task = {"task_id": task_id, "request": request[:1000],
                             "plan": [s.get("objective") for s in (plan or {}).get("steps", [])]}
        return await self.save(state)
