"""Análisis de gastos genérico (gastos comunes, presupuestos, listados de costos) a partir de uno o más CSV.

Sin estado: el contenido del archivo se analiza en memoria y NO se guarda (suele traer nombres y sueldos).
En la auditoría solo quedan metadatos (cantidad de archivos, partidas, períodos y hallazgos).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import tenant_tx
from ..expenses.analyzer import analyze
from ..expenses.parser import ExpenseFormatError, parse_statements
from ..schemas import ExpenseAnalyzeIn
from ..security import SCAN, Principal, require
from ..services import audit

router = APIRouter(tags=["expenses"])


@router.post("/expenses/analyze")
def analyze_expenses(body: ExpenseAnalyzeIn, p: Principal = Depends(require(*SCAN))):
    statements, errors = [], []
    for f in body.files:
        try:
            statements.extend(parse_statements(f.csv_text, f.filename))
        except ExpenseFormatError as exc:
            errors.append(f"{f.filename}: {exc}")
    if errors:
        raise HTTPException(422, {"message": "No se pudo interpretar el archivo", "errors": errors})
    result = analyze(statements)
    with tenant_tx(p.org_id) as conn:
        audit.record(conn, p.org_id, audit.EXPENSES_ANALYZED, actor=audit.user_actor(p), entity_type="expense_analysis",
                     payload={"files": len(body.files), "statements": len(statements),
                              "items": sum(len(s.items) for s in statements),
                              "periods": [s.period_key for s in statements if s.period_key],
                              "findings": result["total_findings"], "checks_failed": result["checks"]["failed"]})
    return result
