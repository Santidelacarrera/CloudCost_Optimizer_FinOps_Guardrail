"""Informe automatizado de optimización de almacenes de datos (Qdrant y PostgreSQL) y de uso frente a presupuesto.

Determinista: mismos datos + mismo `generated_at` → mismo informe byte a byte. Solo lectura: no ejecuta ninguna recomendación;
las acciones SQL/API que aparecen son TEXTO para que un humano las revise.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.budgets import BudgetPolicy, evaluate
from ..domain.rules import RuleConfig, evaluate_all

VERIFICATION = {
    "qdrant": "Validado solo contra respuestas simuladas de la API de Qdrant (nivel L1); no se ha probado contra un servidor real.",
    "postgres": "Validado contra un PostgreSQL 16 real local en pruebas de integración (nivel L2 limitado); no contra RDS/Aurora gestionado.",
}
LIMITATIONS = [
    "Las cifras de ahorro son ESTIMACIONES con fórmulas versionadas (docs/savings-methodology.md); no son ahorro observado.",
    "Estas recomendaciones son consultivas: el sistema no las aprueba, no abre PR ni ejecuta cambios sobre las bases de datos.",
    "Quitar un índice o reducir réplicas puede degradar el rendimiento o la disponibilidad: valida con tráfico representativo antes de aplicar.",
    "En bases gestionadas el ahorro de almacenamiento solo se materializa si el volumen puede reducirse; si no, el beneficio es de rendimiento.",
]


def _row(f) -> dict[str, Any]:
    return {"rule_id": f.rule_id, "action": f.action, "resource": f.resource.name or f.resource.resource_id,
            "service": f.resource.service, "environment": f.resource.environment, "title": f.title, "summary": f.summary,
            "current_monthly_cost": round(f.current_monthly_cost, 2), "projected_monthly_cost": round(f.projected_monthly_cost, 2),
            "estimated_monthly_savings": round(f.estimated_monthly_savings, 2), "confidence": round(f.confidence, 2),
            "destructive": f.destructive, "params": f.params, "evidence": f.evidence}


def build_report(resources: list, *, cfg: RuleConfig | None = None, generated_at: datetime, tenant: str = "",
                 budget: BudgetPolicy | None = None, spent_usd: float = 0.0, warnings: list[str] | None = None,
                 sources: list[str] | None = None) -> dict[str, Any]:
    findings = sorted(evaluate_all(resources, cfg or RuleConfig()),
                      key=lambda f: (-f.estimated_monthly_savings, f.rule_id, f.resource.resource_id))
    rows = [_row(f) for f in findings]
    by_service: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = by_service.setdefault(r["service"], {"findings": 0, "estimated_monthly_savings": 0.0})
        s["findings"] += 1
        s["estimated_monthly_savings"] = round(s["estimated_monthly_savings"] + r["estimated_monthly_savings"], 2)
    inventory_cost = round(sum(float(r.monthly_cost or 0) for r in resources), 2)
    total = round(sum(r["estimated_monthly_savings"] for r in rows), 2)
    report: dict[str, Any] = {
        "generated_at": generated_at.isoformat(timespec="seconds"), "tenant": tenant, "sources": sorted(sources or []),
        "resources_seen": len(resources), "inventory_monthly_cost": inventory_cost, "findings": rows,
        "total_estimated_monthly_savings": total, "by_service": dict(sorted(by_service.items())),
        "verification": {s: VERIFICATION[s] for s in (sources or []) if s in VERIFICATION}, "limitations": LIMITATIONS,
        "warnings": sorted(warnings or []),
    }
    if budget is not None:
        d = evaluate(budget, spent_usd=spent_usd, today=generated_at.date())
        report["budget"] = {"metric": budget.metric, "mode": budget.mode, **d.to_dict()}
    return report


def to_markdown(rep: dict[str, Any]) -> str:
    L: list[str] = ["# Informe de optimización de almacenes de datos", ""]
    L.append(f"Generado: {rep['generated_at']}" + (f" · Tenant: {rep['tenant']}" if rep["tenant"] else ""))
    L.append(f"Fuentes: {', '.join(rep['sources']) or '—'} · Recursos analizados: {rep['resources_seen']} · "
             f"Coste mensual inventariado: USD {rep['inventory_monthly_cost']:.2f}")
    L += ["", f"**Ahorro mensual estimado total: USD {rep['total_estimated_monthly_savings']:.2f}** (estimación, no ahorro observado)", ""]
    if rep["by_service"]:
        L += ["| Servicio | Hallazgos | Ahorro estimado (USD/mes) |", "|---|---:|---:|"]
        L += [f"| {k} | {v['findings']} | {v['estimated_monthly_savings']:.2f} |" for k, v in rep["by_service"].items()]
        L.append("")
    if "budget" in rep:
        b = rep["budget"]
        L += ["## Presupuesto", "", f"Métrica `{b['metric']}` · modo `{b['mode']}` · límite USD {b['limit_usd']:.2f} · gastado USD {b['spent_usd']:.2f} · "
              f"estado **{b['level']}**"]
        L += [f"- {x}" for x in b["reasons"]] + [""]
    L += ["## Recomendaciones", ""]
    if not rep["findings"]:
        L += ["Sin hallazgos con los umbrales actuales.", ""]
    for i, f in enumerate(rep["findings"], 1):
        L += [f"### {i}. {f['title']}", "",
              f"- Recurso: `{f['resource']}` ({f['service']}, {f['environment']})",
              f"- Ahorro estimado: USD {f['estimated_monthly_savings']:.2f}/mes (de USD {f['current_monthly_cost']:.2f} a USD {f['projected_monthly_cost']:.2f}) · "
              f"confianza {f['confidence']:.2f}" + (" · **acción destructiva**" if f["destructive"] else ""),
              f"- {f['summary']}"]
        sql = f["params"].get("sql") if isinstance(f["params"], dict) else None
        if sql:
            L += ["", "```sql", str(sql), "```"]
        L.append("")
    L += ["## Cómo se validó", ""] + [f"- **{k}**: {v}" for k, v in rep["verification"].items()]
    L += ["", "## Límites de este informe", ""] + [f"- {x}" for x in rep["limitations"]]
    if rep["warnings"]:
        L += ["", "## Avisos de recolección", ""] + [f"- {w}" for w in rep["warnings"]]
    return "\n".join(L) + "\n"
