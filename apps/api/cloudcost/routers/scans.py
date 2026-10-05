from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import tenant_tx
from ..schemas import ScanCreate
from ..security import READ, SCAN, Principal, require
from ..services import audit, scan_service

router = APIRouter(tags=["scans"])
log = logging.getLogger(__name__)


@router.post("/scans", status_code=202)
def create_scan(body: ScanCreate, p: Principal = Depends(require(*SCAN))):
    """Encola un escaneo asíncrono (no bloquea la petición HTTP). Consulta el estado con GET /scans/{id}."""
    from ..workers.tasks import run_scan_task            # import perezoso: evita cargar Celery en cada import

    with tenant_tx(p.org_id) as conn:
        account = conn.execute("select id, status from cloud_accounts where id = %s", (str(body.cloud_account_id),)).fetchone()
        if not account or account["status"] != "ACTIVE":
            raise HTTPException(404, "Cuenta cloud no encontrada o deshabilitada")
        if body.repository_id and not conn.execute("select 1 from repositories where id = %s", (str(body.repository_id),)).fetchone():
            raise HTTPException(404, "Repositorio no encontrado")
        scan = conn.execute(
            """insert into scans (organization_id, cloud_account_id, repository_id, requested_by)
               values (%s, %s, %s, %s) returning id, status, timeout_seconds, created_at""",
            (str(p.org_id), str(body.cloud_account_id), str(body.repository_id) if body.repository_id else None, p.user_id)).fetchone()
        audit.record(conn, p.org_id, audit.SCAN_REQUESTED, actor=audit.user_actor(p), entity_type="scan", entity_id=scan["id"],
                     payload={"cloud_account_id": str(body.cloud_account_id),
                              "repository_id": str(body.repository_id) if body.repository_id else None})
    try:
        task = run_scan_task.apply_async(args=[str(p.org_id), str(scan["id"])], soft_time_limit=scan["timeout_seconds"],
                                         time_limit=scan["timeout_seconds"] + 30)
    except Exception as exc:
        log.exception("no se pudo encolar el escaneo")
        scan_service.mark_scan_failed(p.org_id, scan["id"], "queue_unavailable")
        raise HTTPException(503, "La cola de trabajos no está disponible") from exc
    with tenant_tx(p.org_id) as conn:
        conn.execute("update scans set celery_task_id = %s where id = %s", (task.id, str(scan["id"])))
    return {"scan_id": scan["id"], "status": scan["status"]}


@router.get("/scans")
def list_scans(limit: int = Query(20, ge=1, le=100), p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return conn.execute(
            """select id, cloud_account_id, repository_id, status, attempts, max_attempts, error, stats, created_at,
                      started_at, finished_at from scans order by created_at desc limit %s""", (limit,)).fetchall()


@router.get("/scans/{scan_id}")
def get_scan(scan_id: UUID, p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        row = conn.execute(
            """select id, cloud_account_id, repository_id, requested_by, status, attempts, max_attempts, timeout_seconds,
                      error, stats, created_at, started_at, finished_at from scans where id = %s""", (str(scan_id),)).fetchone()
    if not row:
        raise HTTPException(404, "Escaneo no encontrado")
    return row
