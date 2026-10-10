"""Análisis de documentos financieros en CSV: estados de gastos (informes o tablas) y flujos/estados de pago de obra.

`analyze_documents` detecta el tipo de cada archivo, lo interpreta con su lector y junta los hallazgos. Sin estado, sin red y sin IA.
"""
from __future__ import annotations

from decimal import Decimal

from .analyzer import analyze
from .cloud import CLOUD_MAX_ROWS, analyze_cloud, detect_cloud
from .parser import MAX_ROWS, ExpenseFormatError, Statement, parse_statements, read_rows
from .project_flow import analyze_project_flow, looks_like_project_flow, parse_project_flow

__all__ = ["ExpenseFormatError", "Statement", "analyze_documents"]

_ORDER = {"alert": 0, "review": 1, "info": 2}
_MAX_FINDINGS = 100


def analyze_documents(files: list[tuple[str, str]]) -> tuple[dict, dict]:
    """files = [(nombre, texto_csv)]. Devuelve (resultado, errores_por_archivo). Si hay errores no hay resultado útil."""
    statements: list[Statement] = []
    projects: list[dict] = []
    clouds: list[dict] = []
    errors: list[str] = []
    for name, text in files:
        try:
            rows = read_rows(text, max_rows=CLOUD_MAX_ROWS)
            if (exp := detect_cloud(rows, name)) is not None:
                clouds.append(analyze_cloud(exp, name))
                continue
            if len(rows) > MAX_ROWS + 200:
                raise ExpenseFormatError(f"El archivo supera el máximo de {MAX_ROWS} filas")
            if looks_like_project_flow(rows):
                projects.append(analyze_project_flow(parse_project_flow(rows, name)))
            else:
                statements.extend(parse_statements(text, name))
        except ExpenseFormatError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        return {}, {"errors": errors}
    result = analyze(statements) if statements else {"statements": [], "comparison": None, "findings": [], "total_findings": 0,
                                                     "checks": {"passed": 0, "failed": 0}}
    p_findings = [f for p in projects for f in p["findings"]]
    result["findings"] = sorted(result["findings"] + p_findings, key=lambda f: (_ORDER[f["severity"]], -Decimal(f["amount"] or 0)))[:_MAX_FINDINGS]
    result["total_findings"] += len(p_findings)
    p_checks = [c for p in projects for c in p["checks"]]
    result["checks"] = {"passed": result["checks"]["passed"] + sum(1 for c in p_checks if c["ok"]),
                        "failed": result["checks"]["failed"] + sum(1 for c in p_checks if not c["ok"])}
    result["projects"] = projects
    result["cloud"] = clouds
    result["total_findings"] += sum(len(c["findings"]) for c in clouds)
    return result, {}
