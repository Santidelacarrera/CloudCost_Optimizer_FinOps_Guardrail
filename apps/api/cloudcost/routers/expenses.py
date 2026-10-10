"""Análisis de documentos financieros en CSV (gastos comunes, presupuestos, listados de costos, estados de pago de obra).

Sin estado: el contenido del archivo se analiza en memoria y NO se guarda (suele traer nombres y sueldos).
En la auditoría solo quedan metadatos (cantidad de archivos, partidas, períodos y hallazgos).
"""
from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException

from ..db import tenant_tx
from ..expenses import ExpenseFormatError, analyze_documents
from ..expenses.xlsx import xlsx_to_csv_texts
from ..schemas import ExpenseAnalyzeIn
from ..security import SCAN, Principal, require
from ..services import audit

router = APIRouter(tags=["expenses"])


@router.post("/expenses/analyze")
def analyze_expenses(body: ExpenseAnalyzeIn, p: Principal = Depends(require(*SCAN))):
    docs: list[tuple[str, str]] = []
    problems: list[str] = []
    for f in body.files:
        if f.xlsx_base64 is None:
            docs.append((f.filename, f.csv_text or ""))
            continue
        try:
            docs.extend(xlsx_to_csv_texts(base64.b64decode(f.xlsx_base64, validate=True), f.filename))
        except (binascii.Error, ValueError) as exc:
            problems.append(f"{f.filename}: {exc}" if isinstance(exc, ExpenseFormatError) else f"{f.filename}: el contenido no es un Excel válido")
    if problems:
        raise HTTPException(422, {"message": "No se pudo interpretar el archivo", "errors": problems})
    result, err = analyze_documents(docs)
    if err:
        raise HTTPException(422, {"message": "No se pudo interpretar el archivo", "errors": err["errors"]})
    with tenant_tx(p.org_id) as conn:
        audit.record(conn, p.org_id, audit.EXPENSES_ANALYZED, actor=audit.user_actor(p), entity_type="expense_analysis",
                     payload={"files": len(body.files), "statements": len(result["statements"]), "projects": len(result["projects"]),
                              "cloud_exports": len(result["cloud"]), "excel_files": sum(1 for f in body.files if f.xlsx_base64 is not None),
                              "items": sum(st["item_count"] for st in result["statements"]),
                              "periods": [st["period"] for st in result["statements"] if st["period"]],
                              "findings": result["total_findings"], "checks_failed": result["checks"]["failed"]})
    return result
