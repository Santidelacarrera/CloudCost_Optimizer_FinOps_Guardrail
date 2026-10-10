"""Importación de archivos CSV: se valida, se guarda y se encola un escaneo con el mismo flujo de reglas/aprobación/PR."""
from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from psycopg.types.json import Jsonb

from ..collectors.file_import import MAX_ROWS, TEMPLATE_CSV, TEMPLATE_FULL_CSV, parse_csv
from ..db import tenant_tx
from ..expenses.parser import ExpenseFormatError
from ..expenses.xlsx import xlsx_to_csv_texts
from ..schemas import ImportIn
from ..security import READ, SCAN, Principal, require
from ..services import audit, scan_service

router = APIRouter(tags=["imports"])
log = logging.getLogger(__name__)


@router.get("/imports/template", response_class=PlainTextResponse)
def template(full: bool = False, _: Principal = Depends(require(*READ))):
    """`?full=true` incluye ejemplos de RDS, Azure, GCP y Kubernetes además de EC2/EBS/snapshots."""
    return PlainTextResponse(TEMPLATE_FULL_CSV if full else TEMPLATE_CSV, media_type="text/csv")


@router.post("/imports", status_code=202)
def upload(body: ImportIn, p: Principal = Depends(require(*SCAN))):
    """Valida el CSV y encola un escaneo. Si hay errores de formato no se guarda nada y se devuelven todos."""
    from ..workers.tasks import run_scan_task

    text = body.csv_text
    if body.xlsx_base64 is not None:
        try:
            text = xlsx_to_csv_texts(base64.b64decode(body.xlsx_base64, validate=True), body.filename)[0][1]       # primera hoja con datos
        except (binascii.Error, ValueError) as exc:
            msg = str(exc) if isinstance(exc, ExpenseFormatError) else "El contenido no es un Excel válido"
            raise HTTPException(422, {"message": "El archivo tiene errores", "errors": [msg], "total_errors": 1}) from exc
    parsed = parse_csv(text or "")
    if parsed.errors:
        raise HTTPException(422, {"message": "El archivo tiene errores", "errors": parsed.errors[:50], "total_errors": len(parsed.errors)})
    with tenant_tx(p.org_id) as conn:
        if body.repository_id and not conn.execute("select 1 from repositories where id = %s", (str(body.repository_id),)).fetchone():
            raise HTTPException(404, "Repositorio no encontrado")
        account = conn.execute(
            """insert into cloud_accounts (organization_id, provider, account_ref, display_name)
               values (%s, 'import', 'file-import', 'Archivo importado')
               on conflict (organization_id, provider, account_ref) do update set display_name = excluded.display_name
               returning id""", (str(p.org_id),)).fetchone()
        imp = conn.execute(
            """insert into imports (organization_id, cloud_account_id, filename, row_count, rows, created_by)
               values (%s, %s, %s, %s, %s, %s) returning id""",
            (str(p.org_id), str(account["id"]), body.filename, len(parsed.rows), Jsonb(parsed.rows), p.user_id)).fetchone()
        scan = conn.execute(
            """insert into scans (organization_id, cloud_account_id, repository_id, requested_by)
               values (%s, %s, %s, %s) returning id, status, timeout_seconds""",
            (str(p.org_id), str(account["id"]), str(body.repository_id) if body.repository_id else None, p.user_id)).fetchone()
        audit.record(conn, p.org_id, audit.IMPORT_UPLOADED, actor=audit.user_actor(p), entity_type="import", entity_id=imp["id"],
                     payload={"filename": body.filename, "rows": len(parsed.rows), "max_rows": MAX_ROWS, "scan_id": str(scan["id"])})
    try:
        task = run_scan_task.apply_async(args=[str(p.org_id), str(scan["id"])], soft_time_limit=scan["timeout_seconds"],
                                         time_limit=scan["timeout_seconds"] + 30)
    except Exception as exc:
        log.exception("no se pudo encolar el escaneo de importación")
        scan_service.mark_scan_failed(p.org_id, scan["id"], "queue_unavailable")
        raise HTTPException(503, "La cola de trabajos no está disponible") from exc
    with tenant_tx(p.org_id) as conn:
        conn.execute("update scans set celery_task_id = %s where id = %s", (task.id, str(scan["id"])))
    return {"import_id": imp["id"], "scan_id": scan["id"], "rows": len(parsed.rows), "status": scan["status"]}
