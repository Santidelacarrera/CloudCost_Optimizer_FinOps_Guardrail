"""Tareas asíncronas. Cada job tiene ID (scan), estado, reintentos acotados, timeout y trazabilidad (audit_events)."""
from __future__ import annotations

import logging
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded

from ..config import get_settings
from ..db import tenant_tx
from ..llm.client import build_client
from ..secrets import SecretResolver
from ..services import scan_service
from .celery_app import celery_app

log = logging.getLogger(__name__)
_NON_RETRYABLE = (PermissionError, NotImplementedError, LookupError)


def _llm_client():
    s = get_settings()
    if not s.llm_enabled:
        return None
    return build_client(s.llm_provider, s.llm_model,
                        s.openai_api_key.get_secret_value() if s.openai_api_key else None,
                        s.gemini_api_key.get_secret_value() if s.gemini_api_key else None, s.llm_timeout_seconds)


def _attempts(org_id: UUID, scan_id: UUID) -> tuple[int, int]:
    with tenant_tx(org_id) as conn:
        row = conn.execute("select attempts, max_attempts from scans where id = %s", (str(scan_id),)).fetchone()
    return (row["attempts"], row["max_attempts"]) if row else (1, 1)


@celery_app.task(bind=True, name="cloudcost.run_scan", max_retries=None)
def run_scan_task(self, org_id: str, scan_id: str) -> dict:
    org, scan = UUID(org_id), UUID(scan_id)
    try:
        return scan_service.run_scan(org, scan, settings=get_settings(), secrets=SecretResolver(), llm_client=_llm_client())
    except SoftTimeLimitExceeded:
        scan_service.mark_scan_failed(org, scan, "Tiempo límite excedido", status="TIMED_OUT")
        return {"status": "TIMED_OUT"}
    except Exception as exc:
        log.exception("scan fallido", extra={"scan_id": scan_id, "org_id": org_id})
        attempts, max_attempts = _attempts(org, scan)
        if isinstance(exc, _NON_RETRYABLE) or attempts >= max_attempts:
            scan_service.mark_scan_failed(org, scan, f"{type(exc).__name__}: {exc}")
            return {"status": "FAILED"}
        scan_service.mark_scan_retrying(org, scan, f"{type(exc).__name__}: {exc}")
        raise self.retry(exc=exc, countdown=min(30 * 2 ** (attempts - 1), 600)) from exc
