"""Datos del informe ejecutivo. Solo lectura, dentro de la transacción del tenant (RLS aplica a todas las consultas)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg import Connection

from ..services.dashboard import _OPEN, breakdown, summary

RULE_LABELS = {
    "ec2_downsize": "Instancias sobredimensionadas",
    "ec2_idle": "Instancias ociosas",
    "ebs_orphan": "Volúmenes huérfanos",
    "snapshot_old": "Snapshots antiguos",
}
STATUS_LABELS = {
    "PENDING_APPROVAL": "Pendiente de aprobación", "APPROVED": "Aprobada", "PR_CREATED": "PR creado",
    "MERGED": "PR fusionado", "DEPLOYED": "Desplegada", "VERIFIED": "Ahorro verificado", "REJECTED": "Rechazada",
    "DETECTED": "Detectada", "ANALYZED": "Analizada", "PROPOSED": "Propuesta", "EXPIRED": "Caducada",
}
ATTRIBUTION_LABELS = {
    "confirmed": "Coherente con el cambio", "partial": "Menor de lo esperado", "exceeds_model": "Mayor de lo que explica el cambio",
    "not_applied": "El cambio no parece aplicado", "confounded": "No atribuible solo al cambio", "inconclusive": "No concluyente",
    "unverified": "Declarado, no medido",
}
GRADE_LABELS = {"high": "Alta", "medium": "Media", "low": "Baja"}
RISK_LABELS = {"LOW": "Bajo", "MEDIUM": "Medio", "HIGH": "Alto"}
ITEM_LIMIT = 1000


def rule_label(rule_id: str) -> str:
    return RULE_LABELS.get(rule_id, rule_id)


def build(conn: Connection, org_id: Any, *, now: datetime | None = None) -> dict[str, Any]:
    org = conn.execute("select name from organizations where id = %s", (str(org_id),)).fetchone()
    kpi = summary(conn)
    by_rule = conn.execute(f"""
        select rule_id, count(*) as n, coalesce(sum(estimated_monthly_savings), 0) as savings,
               coalesce(sum(current_monthly_cost), 0) as cost
          from recommendations where status in {_OPEN} group by rule_id order by savings desc""").fetchall()
    by_env = conn.execute(f"""
        select res.environment, count(*) as n, coalesce(sum(r.estimated_monthly_savings), 0) as savings
          from recommendations r join resources res on res.id = r.resource_pk
         where r.status in {_OPEN} group by res.environment order by savings desc""").fetchall()
    by_risk = conn.execute(f"""
        select risk, count(*) as n, coalesce(sum(estimated_monthly_savings), 0) as savings
          from recommendations where status in {_OPEN} group by risk order by savings desc""").fetchall()
    items = conn.execute(f"""
        select r.id, r.rule_id, r.title, r.status, r.risk, r.priority, r.confidence, r.destructive,
               r.current_monthly_cost, r.projected_monthly_cost, r.estimated_monthly_savings, r.created_at,
               res.resource_id, res.name as resource_name, res.environment, res.service, res.region, res.cost_source
          from recommendations r join resources res on res.id = r.resource_pk
         where r.status in {_OPEN} order by r.estimated_monthly_savings desc, r.created_at limit {ITEM_LIMIT + 1}""").fetchall()
    verified = conn.execute("""
        select r.title, sv.expected_monthly_savings, sv.observed_monthly_savings, sv.raw_observed_monthly_savings, sv.realization_pct,
               sv.method, sv.window_start, sv.window_end, sv.created_at, sv.data_grade, sv.attribution, sv.confidence_grade,
               sv.confounders, sv.estimated_monthly_savings
          from savings_verifications sv join recommendations r on r.id = sv.recommendation_id
         order by sv.created_at desc limit 200""").fetchall()
    truncated = len(items) > ITEM_LIMIT
    return {
        "organization": (org or {}).get("name") or "Organización",
        "generated_at": (now or datetime.now(timezone.utc)).replace(microsecond=0),
        "kpi": {k: kpi[k] for k in ("monthly_spend", "potential_savings", "savings_pct", "recommendations", "pending_approval",
                                    "high_risk", "verified", "rejected", "realized_savings", "expected_savings_verified",
                                    "realization_pct")},
        "by_rule": [{"rule_id": r["rule_id"], "label": rule_label(r["rule_id"]), "count": r["n"],
                     "savings": float(r["savings"]), "cost": float(r["cost"])} for r in by_rule],
        "by_environment": [{"environment": r["environment"], "count": r["n"], "savings": float(r["savings"])} for r in by_env],
        "by_risk": [{"risk": r["risk"], "label": RISK_LABELS.get(r["risk"], r["risk"]), "count": r["n"],
                     "savings": float(r["savings"])} for r in by_risk],
        "items": [{**i, "rule_label": rule_label(i["rule_id"]), "status_label": STATUS_LABELS.get(i["status"], i["status"]),
                   "risk_label": RISK_LABELS.get(i["risk"], i["risk"]),
                   "confidence": float(i["confidence"]), "current_monthly_cost": float(i["current_monthly_cost"]),
                   "projected_monthly_cost": float(i["projected_monthly_cost"]),
                   "estimated_monthly_savings": float(i["estimated_monthly_savings"])} for i in items[:ITEM_LIMIT]],
        "items_truncated": truncated,
        "breakdown": breakdown(conn),
        "verified": [{**v, "expected_monthly_savings": float(v["expected_monthly_savings"]),
                      "observed_monthly_savings": float(v["observed_monthly_savings"]),
                      "raw_observed_monthly_savings": float(v["raw_observed_monthly_savings"]) if v["raw_observed_monthly_savings"] is not None else None,
                      "estimated_monthly_savings": float(v["estimated_monthly_savings"]) if v["estimated_monthly_savings"] is not None else None,
                      "attribution_label": ATTRIBUTION_LABELS.get(v["attribution"], v["attribution"]),
                      "grade_label": GRADE_LABELS.get(v["confidence_grade"], v["confidence_grade"]),
                      "confounders": [c["code"] for c in (v["confounders"] or [])],
                      "realization_pct": float(v["realization_pct"]) if v["realization_pct"] is not None else None}
                     for v in verified],
    }
