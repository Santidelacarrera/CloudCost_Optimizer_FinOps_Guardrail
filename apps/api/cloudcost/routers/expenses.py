"""Análisis de documentos financieros en CSV (gastos comunes, presupuestos, listados de costos, estados de pago de obra).

Sin estado: el contenido del archivo se analiza en memoria y NO se guarda (suele traer nombres y sueldos).
En la auditoría solo quedan metadatos (cantidad de archivos, partidas, períodos y hallazgos).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import tenant_tx
from ..expenses import analyze_documents
from ..schemas import ExpenseAnalyzeIn
from ..security import SCAN, Principal, require
from ..services import audit

router = APIRouter(tags=["expenses"])


@router.post("/expenses/analyze")
def analyze_expenses(body: ExpenseAnalyzeIn, p: Principal = Depends(require(*SCAN))):
    result, err = analyze_documents([(f.filename, f.csv_text) for f in body.files])
    if err:
        raise HTTPException(422, {"message": "No se pudo interpretar el archivo", "errors": err["errors"]})
    with tenant_tx(p.org_id) as conn:
        audit.record(conn, p.org_id, audit.EXPENSES_ANALYZED, actor=audit.user_actor(p), entity_type="expense_analysis",
                     payload={"files": len(body.files), "statements": len(result["statements"]), "projects": len(result["projects"]),
                              "items": sum(st["item_count"] for st in result["statements"]),
                              "periods": [st["period"] for st in result["statements"] if st["period"]],
                              "findings": result["total_findings"], "checks_failed": result["checks"]["failed"]})
    return result
