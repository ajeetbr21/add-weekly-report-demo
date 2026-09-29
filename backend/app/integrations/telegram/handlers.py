"""Telegram update handling (pure functions over an Atlas API client + a message sender).

Commands: /start /help /status /tasks /agents /approvals /feedback <TASK-ID> <text>
Natural-language messages become tasks. Approvals and feedback use inline keyboard buttons.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.integrations.telegram.api_client import AtlasApiClient

logger = logging.getLogger("atlas.telegram")
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
HELP = """Atlas - local multi-agent assistant
Send any request in plain language, e.g. "Check today's AWS alarms".

/status - system health and task counts
/tasks - your recent tasks
/agents - available agents
/approvals - pending approvals
/feedback TASK-2026-000001 <text> - give feedback on a task
/help - this message"""


class Sender(Protocol):
    async def send(self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None) -> None: ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...


@dataclass
class Ctx:
    api: AtlasApiClient
    sender: Sender
    allowed_chat_ids: list[int]
    poll_seconds: float = 3.0
    max_wait_seconds: float = 900.0


def _allowed(ctx: Ctx, chat_id: int) -> bool:
    return not ctx.allowed_chat_ids or chat_id in ctx.allowed_chat_ids


def format_result(task: dict[str, Any]) -> str:
    res = task.get("result") or {}
    lines = [f"{task['id']} - {task['status']}"]
    if task["status"] == "FAILED":
        lines.append(f"Error: {task.get('error') or 'unknown'}")
    if res.get("summary"):
        lines.append(res["summary"][:2500])
    for f in (res.get("findings") or [])[:6]:
        lines.append(f"• {f[:300]}")
    for r in (res.get("recommendations") or [])[:4]:
        lines.append(f"→ {r[:300]}")
    for c in (res.get("configuration_required") or [])[:3]:
        lines.append(f"⚠ {c[:300]}")
    if res.get("report_id"):
        lines.append(f"Report: {res['report_id']} (see Web UI → Reports)")
    return "\n".join(lines)[:4000]


def approval_buttons(approval_id: str) -> list[list[dict[str, str]]]:
    return [[{"text": "✅ APPROVE", "callback_data": f"appr:{approval_id}:y"},
             {"text": "❌ REJECT", "callback_data": f"appr:{approval_id}:n"}]]


def feedback_buttons(task_id: str) -> list[list[dict[str, str]]]:
    return [[{"text": "👍 Worked", "callback_data": f"fb:{task_id}:p"},
             {"text": "👎 Incorrect", "callback_data": f"fb:{task_id}:n"}]]


async def follow_task(ctx: Ctx, chat_id: int, task_id: str) -> dict[str, Any]:
    """Poll the API until the task finishes; surface approval requests on the way."""
    announced: set[str] = set()
    waited = 0.0
    task: dict[str, Any] = {}
    while waited <= ctx.max_wait_seconds:
        task = await ctx.api.get_task(task_id)
        if task["status"] == "WAITING_APPROVAL":
            for appr in await ctx.api.approvals(task_id=task_id):
                if appr["id"] not in announced:
                    announced.add(appr["id"])
                    await ctx.sender.send(chat_id, f"Approval required for {task_id}\nAction: {appr['action_summary']}"
                                                   f"\nRisk: {appr['risk_level']}", approval_buttons(appr["id"]))
        if task["status"] in TERMINAL:
            await ctx.sender.send(chat_id, format_result(task),
                                  feedback_buttons(task_id) if task["status"] == "COMPLETED" else None)
            return task
        await asyncio.sleep(ctx.poll_seconds)
        waited += ctx.poll_seconds
    await ctx.sender.send(chat_id, f"{task_id} is still {task.get('status')}. Use /tasks to check later.")
    return task


async def handle_message(ctx: Ctx, message: dict[str, Any]) -> asyncio.Task | None:
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return None
    if not _allowed(ctx, chat_id):
        await ctx.sender.send(chat_id, "This chat is not authorized for Atlas.")
        return None
    user = (message.get("from") or {}).get("username")
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    if cmd in ("/start", "/help"):
        await ctx.sender.send(chat_id, HELP)
    elif cmd == "/status":
        dash, ready = await ctx.api.dashboard(), await ctx.api.health()
        by = dash["tasks"]["by_status"]
        await ctx.sender.send(chat_id, f"System: {ready.get('status')}\nActive tasks: {dash['tasks']['active']}\n"
                                       f"Completed: {by.get('COMPLETED', 0)}  Failed: {by.get('FAILED', 0)}\n"
                                       f"Pending approvals: {dash['pending_approvals']}\n"
                                       f"Approved lessons: {dash['approved_lessons']}")
    elif cmd == "/tasks":
        tasks = await ctx.api.list_tasks(limit=8)
        await ctx.sender.send(chat_id, "\n".join(f"{t['id']} {t['status']} - {t['original_request'][:60]}"
                                                 for t in tasks) or "No tasks yet.")
    elif cmd == "/agents":
        agents = await ctx.api.list_agents()
        await ctx.sender.send(chat_id, "\n".join(f"• {a['name']} ({a['id']}, {a['status']}): {a['description'][:90]}"
                                                 for a in agents))
    elif cmd == "/approvals":
        pending = await ctx.api.approvals()
        if not pending:
            await ctx.sender.send(chat_id, "No pending approvals.")
        for a in pending[:5]:
            await ctx.sender.send(chat_id, f"{a['task_id']}: {a['action_summary']}\nRisk: {a['risk_level']}",
                                  approval_buttons(a["id"]))
    elif cmd == "/feedback":
        m = re.match(r"/feedback(?:@\w+)?\s+(TASK-\d{4}-\d+)\s+(.+)", text, re.S | re.I)
        if not m:
            await ctx.sender.send(chat_id, "Usage: /feedback TASK-2026-000001 <your feedback>")
        else:
            res = await ctx.api.feedback(m.group(1).upper(), m.group(2), chat_id)
            n = len(res.get("candidates") or [])
            await ctx.sender.send(chat_id, f"Thanks - feedback stored. {n} learning candidate(s) created"
                                           + (" (review them in the Web UI → Learning)." if n else "."))
    elif cmd:
        await ctx.sender.send(chat_id, "Unknown command. /help")
    else:
        task = await ctx.api.create_task(text, chat_id, user)
        await ctx.sender.send(chat_id, f"Task {task['id']} created. I'll report back when it's done.")
        return asyncio.create_task(follow_task(ctx, chat_id, task["id"]))
    return None


async def handle_callback(ctx: Ctx, cb: dict[str, Any]) -> None:
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data") or ""
    who = (cb.get("from") or {}).get("username") or str(chat_id)
    if not _allowed(ctx, chat_id):
        await ctx.sender.answer_callback(cb["id"], "Not authorized")
        return
    if data.startswith("appr:"):
        _, approval_id, yn = data.split(":")
        try:
            res = await ctx.api.decide(approval_id, yn == "y", f"telegram:{who}")
            await ctx.sender.answer_callback(cb["id"], res["status"])
            await ctx.sender.send(chat_id, f"Action {res['status']}: {res['action_summary']}")
        except Exception as exc:  # noqa: BLE001
            await ctx.sender.answer_callback(cb["id"], f"Failed: {str(exc)[:80]}")
    elif data.startswith("fb:"):
        _, task_id, pn = data.split(":")
        await ctx.api.feedback(task_id, "This solution worked." if pn == "p" else "That was incorrect.", chat_id,
                               rating="POSITIVE" if pn == "p" else "NEGATIVE")
        await ctx.sender.answer_callback(cb["id"], "Feedback stored")
        if pn == "n":
            await ctx.sender.send(chat_id, f"Sorry. Tell me what to do differently next time with:\n"
                                           f"/feedback {task_id} <what should change>")
