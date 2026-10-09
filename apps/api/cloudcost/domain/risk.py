"""Clasificación de riesgo, impacto y prioridad de una recomendación."""
from __future__ import annotations

from .rules import ACTION_DELETE_SNAPSHOT, ACTION_RESIZE, ACTION_RIGHTSIZE_WORKLOAD, Finding

LOW, MEDIUM, HIGH = "LOW", "MEDIUM", "HIGH"
PROTECTED_ENVIRONMENTS = ("production", "unknown")     # lo desconocido se trata como producción (defensivo)


def is_production_like(environment: str | None) -> bool:
    return (environment or "unknown") in PROTECTED_ENVIRONMENTS


def classify_risk(finding: Finding) -> str:
    res = finding.resource
    prod = is_production_like(res.environment)

    if finding.action == ACTION_RIGHTSIZE_WORKLOAD:
        # Bajar requests reinicia pods (rolling update) pero no borra nada: como un resize. Sin HPA ni OOM la regla ya lo descartó.
        return LOW if (not prod and finding.confidence >= 0.85) else MEDIUM

    # HIGH: bases de datos, redes, clusters críticos y cambios arquitectónicos
    if res.resource_type in ("database", "kubernetes") or res.service in ("rds", "vpc", "eks"):
        return HIGH
    if finding.action == ACTION_DELETE_SNAPSHOT:
        return LOW                                   # recursos claramente abandonados (reglas ya filtran AMI/backup)
    if finding.action == ACTION_RESIZE:
        if not prod and finding.confidence >= 0.85:
            return LOW                               # cambio no productivo de bajo impacto
        return MEDIUM
    if finding.rule_id == "ec2_idle":
        return HIGH if prod else MEDIUM              # eliminar cómputo en producción
    return MEDIUM                                    # almacenamiento: eliminar volumen, etc.


def financial_impact(monthly_savings: float) -> str:
    return HIGH if monthly_savings >= 200 else MEDIUM if monthly_savings >= 50 else LOW


def priority(monthly_savings: float, confidence: float, risk: str) -> str:
    weight = {LOW: 1.0, MEDIUM: 0.7, HIGH: 0.4}[risk]
    score = monthly_savings * confidence * weight
    return "P1" if score >= 200 else "P2" if score >= 50 else "P3"
