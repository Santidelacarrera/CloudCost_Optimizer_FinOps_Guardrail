"""Texto del Pull Request: ahorro, riesgo, confianza, evidencia, recomendación y validaciones."""
from __future__ import annotations

import re
from typing import Any


def _money(v: Any) -> str:
    return f"USD {float(v):,.2f}"


def branch_name(rec: dict[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(rec.get("rule_id", "finops")).lower()).strip("-")
    return f"finops/{str(rec['id'])[:8]}-{slug}"


def pr_title(rec: dict[str, Any]) -> str:
    return f"[FinOps] {rec['title']} (≈ {_money(rec['estimated_monthly_savings'])}/mes)"


def commit_message(rec: dict[str, Any]) -> str:
    return f"finops: {rec['title']}\n\nRecomendación {rec['id']} (riesgo {rec['risk']}, confianza {float(rec['confidence']):.0%})."


def pr_body(rec: dict[str, Any], *, patch_summary: str, validations: list[dict[str, Any]],
            approvals: list[dict[str, Any]], dashboard_url: str | None = None, policy_notes: list[str] | None = None) -> str:
    monthly = float(rec["estimated_monthly_savings"])
    policy = rec.get("policy") or {}
    lines = [
        f"## {rec['title']}",
        "",
        rec["summary"],
        "",
        "### Impacto financiero",
        "| | |",
        "|---|---|",
        f"| Costo actual | {_money(rec['current_monthly_cost'])}/mes |",
        f"| Costo proyectado | {_money(rec['projected_monthly_cost'])}/mes |",
        f"| **Ahorro estimado** | **{_money(monthly)}/mes · {_money(monthly * 12)}/año** |",
        "",
        "### Riesgo y confianza",
        f"- Riesgo: **{rec['risk']}** · Confianza: **{float(rec['confidence']):.0%}** · Prioridad: {rec['priority']}",
        f"- Aprobaciones requeridas: {rec['approvals_required']}"
        + (" (aprobación reforzada)" if policy.get("reinforced") else ""),
    ]
    if rec.get("destructive"):
        lines.append("- ⚠️ Cambio **destructivo**: revisa el `terraform plan` antes de fusionar.")
    if rec.get("automation_blocked"):
        lines.append("- 🔒 Automatización bloqueada por política: este PR es un **borrador** y requiere despliegue manual.")
    lines += ["", "### Cambio propuesto", f"`{patch_summary}`", "", "### Evidencia"]
    for key, value in (rec.get("evidence") or {}).items():
        if not isinstance(value, (dict, list)):
            lines.append(f"- {key}: {value}")
    lines += ["", "### Validaciones previas"]
    for v in validations:
        lines.append(f"- {'✅' if v.get('passed') else '❌'} {v.get('check')}: {v.get('detail', '')}")
    if policy_notes:
        lines += ["", "### Política (OPA)", *[f"- {n}" if not n.startswith("  ") else n for n in policy_notes]]
    lines += ["", "### Aprobaciones"]
    for a in approvals:
        lines.append(f"- {a['approver_role']} · {a.get('email') or a['user_id']} · {a['created_at']:%Y-%m-%d %H:%M UTC} — «{a['reason']}»")
    lines += ["", "---",
              f"Recomendación `{rec['id']}`" + (f" · [Ver en CloudCost Optimizer]({dashboard_url})" if dashboard_url else ""),
              "Generado por CloudCost Optimizer. **No se fusiona ni despliega automáticamente**: el pipeline de CI "
              "(terraform plan + políticas OPA + validación de costos) y la revisión humana siguen siendo obligatorios."]
    return "\n".join(lines)
