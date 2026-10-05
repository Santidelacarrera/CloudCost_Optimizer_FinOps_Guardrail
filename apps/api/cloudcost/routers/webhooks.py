from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..config import Settings, get_settings
from ..db import tenant_tx
from ..services import workflow

router = APIRouter(tags=["webhooks"])
MAX_BODY = 1_000_000


@router.post("/webhooks/github/{org_id}")
async def github_webhook(org_id: UUID, request: Request, settings: Settings = Depends(get_settings)):
    """Webhook de GitHub (evento pull_request). Se autentica con HMAC SHA-256; `org_id` solo fija el tenant (RLS)."""
    secret = settings.github_webhook_secret.get_secret_value() if settings.github_webhook_secret else None
    if not secret:
        raise HTTPException(503, "Webhook no configurado (GITHUB_WEBHOOK_SECRET)")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Payload demasiado grande")
    if not workflow.verify_github_signature(secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Firma inválida")
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return {"ok": True}
    if event != "pull_request":
        return {"handled": False, "reason": "ignored_event"}
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "JSON inválido") from exc

    def process():
        with tenant_tx(org_id) as conn:
            return workflow.handle_pull_request_event(conn, org_id, payload)

    return await run_in_threadpool(process)
