"""Informes ejecutivos descargables (PDF / Excel). Solo lectura; cada descarga queda en la auditoría (sin el contenido)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response

from ..db import tenant_tx
from ..reports import data as report_data
from ..reports.render import PDF_MIME, XLSX_MIME, render_pdf, render_xlsx
from ..security import READ, Principal, require
from ..services import audit

router = APIRouter(tags=["reports"])


@router.get("/reports/executive")
def executive_report(fmt: str = Query("pdf", alias="format", pattern="^(pdf|xlsx)$"),
                     p: Principal = Depends(require(*READ))):
    """Resumen financiero para la gerencia: ahorro potencial y verificado, desglose por regla, entorno y riesgo."""
    now = datetime.now(timezone.utc)
    with tenant_tx(p.org_id) as conn:
        data = report_data.build(conn, p.org_id, now=now)
        audit.record(conn, p.org_id, audit.REPORT_EXPORTED, actor=audit.user_actor(p), entity_type="report",
                     payload={"format": fmt, "recommendations": len(data["items"]),
                              "potential_savings": data["kpi"]["potential_savings"]})
    content, mime = (render_pdf(data), PDF_MIME) if fmt == "pdf" else (render_xlsx(data), XLSX_MIME)
    return Response(content, media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="informe-ejecutivo-{now:%Y-%m-%d}.{fmt}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
