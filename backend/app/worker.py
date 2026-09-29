"""Atlas worker: executes queued tasks, runs the scheduler and recovers interrupted tasks.

One lightweight process (asyncio) with bounded concurrency:
  - claims runnable tasks from PostgreSQL (FOR UPDATE SKIP LOCKED) and runs them through the Orchestrator
  - keeps leases alive with heartbeats; tasks of a crashed worker are recovered when their lease expires
    (or immediately at startup when they were owned by this host's previous incarnation)
  - fires due schedules (persistent scheduler) every SCHEDULER_INTERVAL_SECONDS
  - refreshes the tool registry from MCP providers every TOOL_REFRESH_SECONDS

Run:  python -m app.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid

from sqlalchemy import update

from app.agents.registry import sync_builtin_agents
from app.core.config import get_settings
from app.core.enums import IN_FLIGHT_STATUSES, EventType, StepStatus, TaskStatus
from app.core.logging import configure_logging
from app.database.session import dispose_engine, session_scope
from app.models import Task
from app.orchestrator.orchestrator import Orchestrator
from app.scheduler import service as scheduler
from app.services import journal
from app.tasks import service as task_service
from app.tools.registry import get_tool_registry

logger = logging.getLogger("atlas.worker")


class Worker:
    def __init__(self, orchestrator: Orchestrator | None = None, concurrency: int | None = None):
        self.settings = get_settings()
        self.host = socket.gethostname()
        self.worker_id = f"{self.host}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.orchestrator = orchestrator or Orchestrator(worker_id=self.worker_id)
        self.orchestrator.worker_id = self.worker_id
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.running: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    async def startup(self) -> None:
        async with session_scope() as s:
            await sync_builtin_agents(s)
            await task_service.ensure_default_project(s)
            await journal.system_event(s, "worker", "worker_started", self.worker_id)
        await self.refresh_tools()
        recovered = await self.recover(startup=True)
        if recovered:
            logger.info(f"recovered {len(recovered)} unfinished task(s): {recovered}")

    async def refresh_tools(self) -> None:
        try:
            async with session_scope() as s:
                status = await get_tool_registry().refresh(s)
            logger.info("tool_registry_refreshed", extra={"extra_fields": {"providers": status}})
        except Exception:  # noqa: BLE001
            logger.exception("tool_registry_refresh_failed")

    async def recover(self, startup: bool = False) -> list[str]:
        async with session_scope() as s:
            if startup:
                # tasks locked by a previous incarnation of this worker host are orphaned right now
                res = await s.execute(update(Task).where(
                    Task.status.in_([x.value for x in IN_FLIGHT_STATUSES]), Task.locked_by.like(f"{self.host}:%"),
                    Task.locked_by != self.worker_id).values(lease_expires_at=None))
                _ = res
            return await task_service.recover_stale_tasks(s)

    async def claim(self) -> str | None:
        async with session_scope() as s:
            return await task_service.claim_next_task(s, self.worker_id, self.settings.task_lease_seconds)

    async def _execute(self, task_id: str) -> None:
        hb = asyncio.create_task(self._heartbeat(task_id))
        try:
            status = await self.orchestrator.run(task_id)
            logger.info("task_finished", extra={"extra_fields": {"task_id": task_id, "status": status}})
        except Exception as exc:  # noqa: BLE001 - never crash the worker because of one task
            logger.exception("task_crashed")
            async with session_scope() as s:
                task = await task_service.lock_task(s, task_id)
                await task_service.mark_interrupted(s, task_id, f"worker error: {type(exc).__name__}")
                if not TaskStatus(task.status).is_terminal and task.status != TaskStatus.WAITING_APPROVAL:
                    for st in task.steps:
                        if st.status == StepStatus.RUNNING:
                            st.status = StepStatus.PENDING
                    if task.retry_count < task.max_retries:
                        task.retry_count += 1
                        task.status = TaskStatus.RETRYING
                        await journal.record(s, task.id, EventType.RETRY, f"Crash: {type(exc).__name__}: {exc}"[:500])
                    else:
                        task.status = TaskStatus.FAILED
                        task.error = f"{type(exc).__name__}: {exc}"[:4000]
                        await journal.record(s, task.id, EventType.TASK_FAILED, task.error[:500])
                    await task_service.release(s, task)
        finally:
            hb.cancel()
            self.running.pop(task_id, None)

    async def _heartbeat(self, task_id: str) -> None:
        interval = max(5, self.settings.task_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with session_scope() as s:
                    await task_service.heartbeat(s, task_id, self.worker_id, self.settings.task_lease_seconds)
            except Exception:  # noqa: BLE001
                logger.warning("heartbeat_failed")

    async def run_once(self) -> list[str]:
        """Claim and fully execute runnable tasks sequentially (used by tests / CLI)."""
        done = []
        while (task_id := await self.claim()) is not None:
            self.running[task_id] = asyncio.current_task()  # type: ignore[assignment]
            await self._execute(task_id)
            done.append(task_id)
        return done

    async def run_forever(self) -> None:
        await self.startup()
        last_sched = last_recover = last_tools = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now - last_sched >= self.settings.scheduler_interval_seconds:
                    last_sched = now
                    async with session_scope() as s:
                        fired = await scheduler.tick(s)
                    if fired:
                        logger.info(f"scheduler fired {fired}")
                if now - last_recover >= 30:
                    last_recover = now
                    await self.recover()
                # re-discover tools periodically; much sooner while a configured provider is unreachable
                degraded = any(v.get("status") == "UNREACHABLE"
                               for v in get_tool_registry().provider_status.values())
                if now - last_tools >= (20 if degraded else self.settings.tool_refresh_seconds):
                    last_tools = now
                    await self.refresh_tools()
                while len(self.running) < self.concurrency:
                    task_id = await self.claim()
                    if task_id is None:
                        break
                    self.running[task_id] = asyncio.create_task(self._execute(task_id))
            except Exception:  # noqa: BLE001 - e.g. database temporarily unavailable
                logger.exception("worker_loop_error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.worker_poll_interval_seconds)
            except TimeoutError:
                pass
        logger.info("worker stopping; waiting for running tasks")
        if self.running:
            await asyncio.wait(list(self.running.values()), timeout=20)
        await dispose_engine()

    def stop(self) -> None:
        self._stop.set()


async def _main() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
