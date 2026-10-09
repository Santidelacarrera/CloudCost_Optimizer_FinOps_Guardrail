"""Explicación determinística (siempre disponible, no depende del LLM)."""
from __future__ import annotations

from ..domain.policy import PolicyDecision
from ..domain.rules import Finding

_RISK_TEXT = {
    "LOW": "Riesgo bajo: recurso claramente abandonado o cambio no productivo de bajo impacto.",
    "MEDIUM": "Riesgo medio: modifica o elimina almacenamiento/cómputo; valida la dependencia antes de fusionar.",
    "HIGH": "Riesgo alto: afecta producción, bases de datos, redes o cambios arquitectónicos.",
}


def build_explanation(finding: Finding, risk: str, decision: PolicyDecision) -> str:
    parts = [finding.summary, _RISK_TEXT[risk],
             f"Confianza {finding.confidence:.0%}; ahorro estimado USD {finding.estimated_monthly_savings:,.2f}/mes "
             f"(USD {finding.estimated_monthly_savings * 12:,.2f}/año)."]
    basis = finding.evidence.get("cost_basis") or {}
    if basis.get("verified"):
        parts.append(f"Costo real según {'Cost Explorer' if basis.get('source') == 'cost_explorer' else 'el archivo importado'}"
                     + (f" (últimos {basis['window_days']} días)" if basis.get("window_days") else "") + ".")
    elif basis:
        parts.append("Costo ESTIMADO con tabla de precios (sin costo real disponible): confirma el ahorro con tu factura antes de aprobar.")
    if finding.destructive:
        parts.append("La acción es destructiva: el cambio se propone solo como Pull Request y nunca se ejecuta directamente.")
    if decision.reinforced:
        parts.append(f"Política: aprobación reforzada ({decision.approvals_required} aprobadores, al menos uno ADMIN o SRE); "
                     "el PR se crea como borrador y no habilita automatización.")
    if finding.alternatives:
        parts.append("Alternativas: " + "; ".join(a.get("description", a.get("action", "")) for a in finding.alternatives) )
    return "\n\n".join(parts)
