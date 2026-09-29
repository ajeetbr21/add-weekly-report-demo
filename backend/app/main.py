"""Atlas FastAPI control plane (local only)."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents.registry import sync_builtin_agents
from app.api.deps import require_api_token
from app.api.routes import agents_tools, memory_learning, ops, system, tasks, webhooks
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, correlation_id_var, log_event
from app.database.session import dispose_engine, session_scope
from app.tasks.service import ensure_default_project
from app.tools.registry import get_tool_registry

logger = logging.getLogger("atlas.api")
STATE_CHANGING = ("POST", "PUT", "PATCH", "DELETE")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    try:
        async with session_scope() as db:
            await sync_builtin_agents(db)
            await ensure_default_project(db)
        async with session_scope() as db:
            await get_tool_registry().refresh(db)
    except Exception:  # noqa: BLE001 - API still starts; /ready reports the problem
        logger.exception("startup_initialization_failed")
    yield
    await dispose_engine()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {
        "code": code, "message": message, "details": None, "correlation_id": correlation_id_var.get()}})


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Atlas - Local Multi-Agent AI Platform", version="0.1.0", lifespan=lifespan,
                  description="Local control plane: tasks, agents, tools, memory, learning, approvals, "
                              "schedules, reports and webhooks.")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])
    allowed_origins = set(s.cors_origins)

    # NOTE: Starlette runs the middleware registered LAST first. origin_guard is registered before
    # correlation so that correlation ids + request logs also cover rejected requests.
    @app.middleware("http")
    async def origin_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        """CSRF protection for the local API. Browsers attach an Origin header to cross-site requests:
        state-changing /api calls from an origin other than the Atlas UI are refused, and request bodies
        must be application/json (blocks 'simple' cross-site form/text posts). Webhooks are exempt (they
        are authenticated by signature or token), as are non-browser clients (no Origin header)."""
        path = request.url.path
        if request.method in STATE_CHANGING and path.startswith("/api/") and not path.startswith("/api/webhooks/"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return _error(403, "forbidden_origin", f"Origin {origin} is not allowed")
            has_body = request.headers.get("content-length", "0") not in ("", "0") or \
                "chunked" in request.headers.get("transfer-encoding", "")
            if has_body and "application/json" not in request.headers.get("content-type", ""):
                return _error(415, "unsupported_media_type", "Request body must be application/json")
        return await call_next(request)

    @app.middleware("http")
    async def correlation(request: Request, call_next):  # type: ignore[no-untyped-def]
        cid = (request.headers.get("x-correlation-id") or uuid.uuid4().hex[:16])[:64]
        token = correlation_id_var.set(cid)
        started = time.monotonic()
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = cid
            if request.url.path not in ("/health", "/ready"):
                log_event(logger, "http_request", method=request.method, path=request.url.path,
                          status=response.status_code, duration_ms=int((time.monotonic() - started) * 1000),
                          correlation_id=cid)
            return response
        finally:
            correlation_id_var.reset(token)

    install_error_handlers(app)
    protected = [Depends(require_api_token)]
    app.include_router(system.router)
    app.include_router(webhooks.router)  # signature/token verified inside
    for r in (tasks.router, agents_tools.agents_router, agents_tools.tools_router, memory_learning.memory_router,
              memory_learning.feedback_router, memory_learning.learning_router, ops.approvals_router,
              ops.reports_router, ops.schedules_router, ops.projects_router):
        app.include_router(r, dependencies=protected)
    return app


app = create_app()
