"""Local webhook endpoints. Events become normal Tasks. Idempotent on (source, event_id).

Security: when a secret is configured the HMAC-SHA256 signature is required
(GitHub: X-Hub-Signature-256; custom: X-Atlas-Signature: sha256=<hex of body>). Without a secret
the endpoint requires the normal API token. These endpoints are meant for localhost / tunnels you control.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import _extract, token_valid
from app.core.config import get_settings
from app.core.enums import TaskSource
from app.core.errors import AppError, UnauthorizedError
from app.database.session import get_db
from app.models import WebhookEvent
from app.schemas.api import CustomWebhook, TaskCreate, WebhookAccepted
from app.tasks import service as task_service

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def _authorize(secret: str, body: bytes, sig: str | None, authorization: str | None, api_key: str | None) -> bool:
    if secret:
        if not verify_signature(secret, body, sig):
            raise UnauthorizedError("Invalid webhook signature")
        return True
    if not token_valid(_extract(authorization, api_key)):
        raise UnauthorizedError("Webhook requires a signature secret or the API token")
    return False


async def _accept(db: AsyncSession, source: str, event_id: str, event_type: str | None, payload: dict[str, Any],
                  signature_valid: bool, task_data: TaskCreate | None) -> WebhookAccepted:
    existing = (await db.execute(select(WebhookEvent).where(WebhookEvent.source == source,
                                                            WebhookEvent.event_id == event_id))).scalar_one_or_none()
    if existing:
        return WebhookAccepted(event_id=event_id, task_id=existing.task_id, duplicate=True)
    ev = WebhookEvent(source=source, event_id=event_id, event_type=event_type, payload=payload,
                      signature_valid=signature_valid)
    try:
        async with db.begin_nested():
            db.add(ev)
            await db.flush()
    except IntegrityError:
        dup = (await db.execute(select(WebhookEvent).where(WebhookEvent.source == source,
                                                           WebhookEvent.event_id == event_id))).scalar_one()
        return WebhookAccepted(event_id=event_id, task_id=dup.task_id, duplicate=True)
    if task_data is not None:
        task, _ = await task_service.create_task(db, task_data)
        ev.task_id = task.id
    return WebhookAccepted(event_id=event_id, task_id=ev.task_id, duplicate=False)


def github_request(event: str, p: dict[str, Any]) -> str | None:
    repo = (p.get("repository") or {}).get("full_name", "unknown repo")
    if event == "ping":
        return None
    if event == "push":
        commits = p.get("commits") or []
        msgs = "; ".join(c.get("message", "").split("\n")[0] for c in commits[:5])
        return f"GitHub push to {repo} ({p.get('ref')}): {len(commits)} commit(s): {msgs}. Review the changes."
    if event == "pull_request":
        pr = p.get("pull_request") or {}
        return (f"GitHub pull request {p.get('action')} in {repo}: #{pr.get('number')} '{pr.get('title')}'. "
                f"Review the pull request and summarize risks. URL: {pr.get('html_url')}")
    if event == "issues":
        iss = p.get("issue") or {}
        return f"GitHub issue {p.get('action')} in {repo}: #{iss.get('number')} '{iss.get('title')}'. Triage it."
    if event == "workflow_run":
        run = p.get("workflow_run") or {}
        if run.get("conclusion") != "failure":
            return None
        return f"GitHub workflow '{run.get('name')}' failed in {repo}. Investigate the failure: {run.get('html_url')}"
    return f"GitHub event '{event}' in {repo}. Summarize what happened."


@router.post("/github", response_model=WebhookAccepted, status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request, x_github_event: str = Header(default="unknown"),
                         x_github_delivery: str | None = Header(default=None),
                         x_hub_signature_256: str | None = Header(default=None),
                         authorization: str | None = Header(default=None),
                         x_api_key: str | None = Header(default=None),
                         db: AsyncSession = Depends(get_db)) -> WebhookAccepted:
    body = await request.body()
    signed = _authorize(get_settings().github_webhook_secret.get_secret_value(), body, x_hub_signature_256,
                        authorization, x_api_key)
    if not x_github_delivery:
        raise AppError("Missing X-GitHub-Delivery header")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise AppError("Body must be JSON") from exc
    req = github_request(x_github_event, payload)
    task_data = TaskCreate(request=req, source=TaskSource.WEBHOOK, idempotency_key=f"github:{x_github_delivery}",
                           metadata={"webhook": "github", "event": x_github_event}) if req else None
    return await _accept(db, "github", x_github_delivery, x_github_event, payload, signed, task_data)


@router.post("/custom", response_model=WebhookAccepted, status_code=status.HTTP_202_ACCEPTED)
async def custom_webhook(request: Request, x_atlas_signature: str | None = Header(default=None),
                         authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None),
                         db: AsyncSession = Depends(get_db)) -> WebhookAccepted:
    body = await request.body()
    signed = _authorize(get_settings().custom_webhook_secret.get_secret_value(), body, x_atlas_signature,
                        authorization, x_api_key)
    try:
        data = CustomWebhook.model_validate_json(body or b"{}")
    except ValidationError as exc:
        raise AppError("Invalid webhook payload", status_code=422, code="validation_error",
                       details=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]) from exc
    task_data = TaskCreate(request=data.request, source=TaskSource.WEBHOOK, project=data.project,
                           priority=data.priority, idempotency_key=f"custom:{data.event_id}",
                           metadata={"webhook": "custom", "event_type": data.event_type})
    return await _accept(db, "custom", data.event_id, data.event_type, data.payload, signed, task_data)
