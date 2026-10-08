from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from ..db import tenant_tx
from ..security import READ, Principal, require
from ..services import audit, reports

router = APIRouter(tags=["reports"])

_TYPES = {"pdf": "application/pdf",
          "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


@router.get("/reports/waste")
def waste_report(format: Literal["pdf", "xlsx"] = Query("pdf"), p: Principal = Depends(require(*READ))):
    """Reporte ejecutivo de desperdicio y ahorro proyectado, descargable en PDF o Excel."""
    with tenant_tx(p.org_id) as conn:
        data = reports.load_report_data(conn)
        audit.record(conn, p.org_id, audit.REPORT_EXPORTED, actor=audit.user_actor(p), entity_type="report",
                     payload={"format": format, "opportunities": len(data.opportunities), "monthly_savings": data.potential_monthly})
    body = reports.render_pdf(data) if format == "pdf" else reports.render_xlsx(data)
    name = f"ahorro-{data.generated_at:%Y%m%d}.{format}"
    return Response(content=body, media_type=_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
