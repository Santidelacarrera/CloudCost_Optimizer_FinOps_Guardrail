"""Persistencia de recomendaciones y transiciones de estado."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from ..domain import state_machine as sm
from ..domain.policy import PolicyDecision
from ..domain.rules import Finding
from . import audit
from . import evidence as evidence_store
from .audit import Actor


class WorkflowError(Exception):
    """Error de negocio con código HTTP sugerido (el router lo traduce a una respuesta)."""

    def __init__(self, status: int, message: str, code: str = "error"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def log_transition(conn: Connection, org_id, rec_id, from_status: str | None, to_status: str, actor: Actor,
                   note: str | None = None) -> None:
    conn.execute(
        """insert into recommendation_actions (organization_id, recommendation_id, from_status, to_status, actor_type, actor_id, note)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (str(org_id), str(rec_id), from_status, to_status, actor.type, actor.id, note))


def transition(conn: Connection, org_id, rec_id, current: str, target: str, actor: Actor, note: str | None = None,
               set_sql: str = "", set_params: tuple = ()) -> None:
    """Cambia el estado validando la máquina de estados y con control optimista (status actual esperado)."""
    try:
        sm.assert_transition(current, target)
    except sm.InvalidTransition as exc:
        raise WorkflowError(409, str(exc), "invalid_transition") from exc
    cur = conn.execute(
        f"update recommendations set status = %s, updated_at = now(){(', ' + set_sql) if set_sql else ''} "
        "where id = %s and status = %s",
        (target, *set_params, str(rec_id), current))
    if cur.rowcount != 1:
        raise WorkflowError(409, "La recomendación cambió de estado concurrentemente; recarga e inténtalo de nuevo", "concurrent_update")
    log_transition(conn, org_id, rec_id, current, target, actor, note)


@dataclass
class Upserted:
    rec_id: UUID | None
    created: bool
    version: int | None = None
    evidence_hash: str | None = None
    evidence_new: bool = False


# La versión (que invalida las aprobaciones ya dadas) sube solo ante un cambio MATERIAL: de riesgo, de aprobadores exigidos, de los
# parámetros de la acción o del ahorro en ≥ max(1 USD, 5 %) respecto a lo que veía el aprobador. Con costes reales el ahorro oscila
# unos céntimos en cada escaneo; subir la versión por eso anularía aprobaciones a medias sin que nada importante hubiera cambiado.
VERSION_SAVINGS_TOLERANCE = 0.05
VERSION_SAVINGS_MIN_USD = 1.0

_UPSERT = """
insert into recommendations (
    organization_id, scan_id, cloud_account_id, repository_id, resource_pk, rule_id, action, status, title, summary,
    explanation, alternatives, current_monthly_cost, projected_monthly_cost, estimated_monthly_savings, confidence,
    risk, impact, priority, destructive, automation_blocked, approvals_required, policy, evidence, params, llm_advice,
    dedupe_key)
values (
    %(org)s, %(scan_id)s, %(account_id)s, %(repo_id)s, %(resource_pk)s, %(rule_id)s, %(action)s, %(status)s, %(title)s,
    %(summary)s, %(explanation)s, %(alternatives)s, %(current)s, %(projected)s, %(savings)s, %(confidence)s, %(risk)s,
    %(impact)s, %(priority)s, %(destructive)s, %(blocked)s, %(approvals)s, %(policy)s, %(evidence)s, %(params)s,
    %(llm_advice)s, %(dedupe_key)s)
on conflict (organization_id, dedupe_key) do update set
    scan_id = excluded.scan_id,
    title = excluded.title, summary = excluded.summary, explanation = excluded.explanation,
    alternatives = excluded.alternatives, current_monthly_cost = excluded.current_monthly_cost,
    projected_monthly_cost = excluded.projected_monthly_cost,
    estimated_monthly_savings = excluded.estimated_monthly_savings, confidence = excluded.confidence,
    risk = excluded.risk, impact = excluded.impact, priority = excluded.priority,
    destructive = excluded.destructive, automation_blocked = excluded.automation_blocked,
    approvals_required = excluded.approvals_required, policy = excluded.policy, evidence = excluded.evidence,
    params = excluded.params, llm_advice = excluded.llm_advice,
    status = case when recommendations.status = 'PROPOSED' and excluded.status = 'PENDING_APPROVAL'
                  then 'PENDING_APPROVAL' else recommendations.status end,
    version = recommendations.version + %(bump)s,
    stale_since = null, stale_reason = null,
    updated_at = now()
where recommendations.status in ('PROPOSED', 'PENDING_APPROVAL')
returning id, version, status, (xmax = 0) as created
"""


def _material_change(prev: dict[str, Any], *, risk: str, approvals: int, params: dict, savings: float, basis_savings: float) -> bool:
    if prev["risk"] != risk or int(prev["approvals_required"]) != approvals:
        return True
    if (prev["params"] or {}) != json.loads(json.dumps(params, default=str)):
        return True
    return abs(basis_savings - savings) >= max(VERSION_SAVINGS_MIN_USD, VERSION_SAVINGS_TOLERANCE * max(basis_savings, savings))


def upsert_finding(conn: Connection, org_id, *, scan_id, account_id, repo_id, resource_pk, finding: Finding, risk: str,
                   impact: str, priority: str, decision: PolicyDecision, explanation: str,
                   alternatives: list[dict[str, Any]], evidence: dict[str, Any], llm_advice: dict[str, Any] | None,
                   dedupe_key: str, actor: Actor = audit.SYSTEM) -> Upserted:
    status = sm.PENDING_APPROVAL if decision.allowed else sm.PROPOSED
    prev = conn.execute("select * from recommendations where organization_id = %s and dedupe_key = %s for update",
                        (str(org_id), dedupe_key)).fetchone()
    bump = 0
    if prev is not None and prev["status"] in (sm.PROPOSED, sm.PENDING_APPROVAL):
        basis = evidence_store.latest(conn, prev["id"])
        basis_savings = float(basis["estimated_monthly_savings"]) if basis and basis["recommendation_version"] == prev["version"] \
            else float(prev["estimated_monthly_savings"])
        bump = int(_material_change(prev, risk=risk, approvals=decision.approvals_required, params=finding.params,
                                    savings=finding.estimated_monthly_savings, basis_savings=basis_savings))
    row = conn.execute(_UPSERT, {
        "org": str(org_id), "scan_id": str(scan_id), "account_id": str(account_id),
        "repo_id": str(repo_id) if repo_id else None, "resource_pk": str(resource_pk), "rule_id": finding.rule_id,
        "action": finding.action, "status": status, "title": finding.title, "summary": finding.summary,
        "explanation": explanation, "alternatives": Jsonb(alternatives), "current": finding.current_monthly_cost,
        "projected": finding.projected_monthly_cost, "savings": finding.estimated_monthly_savings,
        "confidence": finding.confidence, "risk": risk, "impact": impact, "priority": priority,
        "destructive": finding.destructive, "blocked": decision.automation_blocked,
        "approvals": decision.approvals_required, "policy": Jsonb(decision.to_dict()), "evidence": Jsonb(evidence),
        "params": Jsonb(finding.params), "llm_advice": Jsonb(llm_advice) if llm_advice else None,
        "dedupe_key": dedupe_key, "bump": bump}).fetchone()
    if row is None:                       # existe en un estado posterior (aprobada, rechazada, PR...): no se toca
        return Upserted(None, False)
    rec_id = row["id"]
    snap = evidence_store.record(conn, org_id, rec_id=rec_id, version=row["version"], scan_id=scan_id, rule_id=finding.rule_id,
                                 params=finding.params, estimated=finding.estimated_monthly_savings,
                                 reference_cost=finding.current_monthly_cost, confidence=finding.confidence, evidence=evidence)
    if row["created"]:
        log_transition(conn, org_id, rec_id, None, sm.DETECTED, actor, "Detectada por regla " + finding.rule_id)
        log_transition(conn, org_id, rec_id, sm.DETECTED, sm.ANALYZED, actor, "Evidencia y riesgo calculados")
        log_transition(conn, org_id, rec_id, sm.ANALYZED, sm.PROPOSED, actor, "Política evaluada")
        if status == sm.PENDING_APPROVAL:
            log_transition(conn, org_id, rec_id, sm.PROPOSED, sm.PENDING_APPROVAL, actor, "A la espera de aprobación humana")
        audit.record(conn, org_id, audit.DETECTION, actor=actor, entity_type="resource", entity_id=resource_pk,
                     payload={"rule_id": finding.rule_id, "evidence": {k: v for k, v in finding.evidence.items() if not isinstance(v, dict)}})
        audit.record(conn, org_id, audit.RECOMMENDATION_CREATED, actor=actor, entity_type="recommendation", entity_id=rec_id,
                     payload={"action": finding.action, "risk": risk, "confidence": finding.confidence,
                              "estimated_monthly_savings": finding.estimated_monthly_savings, "status": status,
                              "approvals_required": decision.approvals_required, "evidence_hash": snap.hash,
                              "formula_id": (evidence.get("estimate") or {}).get("formula_id")})
        return Upserted(rec_id, True, row["version"], snap.hash, snap.created)
    if prev is not None and prev["status"] == sm.PROPOSED and row["status"] == sm.PENDING_APPROVAL:
        log_transition(conn, org_id, rec_id, sm.PROPOSED, sm.PENDING_APPROVAL, actor, "La política ya permite proponerla")
    if snap.created:
        audit.record(conn, org_id, audit.EVIDENCE_UPDATED, actor=actor, entity_type="recommendation", entity_id=rec_id,
                     payload={"version": row["version"], "evidence_hash": snap.hash,
                              "estimated_monthly_savings": finding.estimated_monthly_savings, "version_bumped": bool(bump)})
    return Upserted(rec_id, False, row["version"], snap.hash, snap.created)


# ------------------------------------------------------------------ consultas
LIST_SQL = """
select r.id, r.rule_id, r.action, r.status, r.title, r.summary, r.current_monthly_cost, r.projected_monthly_cost,
       r.estimated_monthly_savings, r.confidence, r.risk, r.impact, r.priority, r.destructive, r.automation_blocked,
       r.approvals_required, r.version, r.created_at, r.updated_at,
       res.resource_id, res.name as resource_name, res.service, res.environment, res.region,
       count(*) over() as total
  from recommendations r
  join resources res on res.id = r.resource_pk
 where (%(status)s::text is null or r.status = %(status)s)
   and (%(risk)s::text is null or r.risk = %(risk)s)
   and (%(min_savings)s::numeric is null or r.estimated_monthly_savings >= %(min_savings)s)
 order by r.estimated_monthly_savings desc, r.created_at desc
 limit %(limit)s offset %(offset)s
"""


def list_recommendations(conn: Connection, *, status=None, risk=None, min_savings=None, limit=50, offset=0) -> dict[str, Any]:
    rows = conn.execute(LIST_SQL, {"status": status, "risk": risk, "min_savings": min_savings,
                                   "limit": limit, "offset": offset}).fetchall()
    total = rows[0]["total"] if rows else 0
    for r in rows:
        r.pop("total", None)
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


def get_detail(conn: Connection, rec_id) -> dict[str, Any]:
    rec = conn.execute(
        """select r.*, res.resource_id, res.name as resource_name, res.service, res.environment, res.region,
                  res.instance_type, res.iac_address, res.iac_file, res.tags as resource_tags
             from recommendations r join resources res on res.id = r.resource_pk where r.id = %s""",
        (str(rec_id),)).fetchone()
    if rec is None:
        raise WorkflowError(404, "Recomendación no encontrada", "not_found")
    rec["approvals"] = conn.execute(
        """select a.id, a.decision, a.reason, a.approver_role, a.recommendation_version, a.created_at, u.email
             from approvals a join users u on u.id = a.user_id where a.recommendation_id = %s order by a.created_at""",
        (str(rec_id),)).fetchall()
    rec["timeline"] = conn.execute(
        """select from_status, to_status, actor_type, actor_id, note, created_at
             from recommendation_actions where recommendation_id = %s order by created_at, id""",
        (str(rec_id),)).fetchall()
    rec["pull_request"] = conn.execute(
        "select number, url, branch, base_branch, draft, state, created_at, merged_at, validations, diff from pull_requests where recommendation_id = %s",
        (str(rec_id),)).fetchone()
    rec["savings_verification"] = conn.execute(
        "select * from savings_verifications where recommendation_id = %s", (str(rec_id),)).fetchone()
    sv = rec["savings_verification"]
    # Tres cifras distintas, nunca mezcladas: la proyección vigente, lo que se aprobó (congelado) y lo que se midió después.
    rec["savings_figures"] = {
        "estimated_monthly": rec["estimated_monthly_savings"],
        "approved_monthly": rec["approved_monthly_savings"], "approved_at": rec["approved_at"],
        "observed_monthly": sv["observed_monthly_savings"] if sv else None,
        "observed_attribution": sv["attribution"] if sv else None, "observed_data_grade": sv["data_grade"] if sv else None,
        "observed_confidence": sv["confidence_grade"] if sv else None}
    rec["evidence_history"] = evidence_store.history(conn, rec_id)
    rec["baselines"] = conn.execute(
        """select id, phase, window_start, window_end, days_with_data, days_expected, monthly_cost, cost_source, data_grade, baseline_hash, created_at
             from savings_baselines where recommendation_id = %s order by created_at""", (str(rec_id),)).fetchall()
    return rec


def get_evidence(conn: Connection, rec_id) -> dict[str, Any]:
    """Todas las versiones de la evidencia con su comprobación de integridad (huella recalculada = huella guardada)."""
    rec = conn.execute("select id, version, evidence_hash, approved_evidence_id, status from recommendations where id = %s", (str(rec_id),)).fetchone()
    if rec is None:
        raise WorkflowError(404, "Recomendación no encontrada", "not_found")
    versions = evidence_store.history(conn, rec_id, with_evidence=True)
    return {"recommendation_id": rec["id"], "version": rec["version"], "current_hash": rec["evidence_hash"],
            "approved_evidence_id": rec["approved_evidence_id"], "all_intact": all(v["integrity_ok"] for v in versions), "versions": versions}
