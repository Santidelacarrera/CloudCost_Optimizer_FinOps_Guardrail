"""Persistencia de recomendaciones y transiciones de estado."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from ..domain import state_machine as sm
from ..domain.policy import PolicyDecision
from ..domain.rules import Finding
from . import audit
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
    llm_advice = excluded.llm_advice,
    version = case when (recommendations.estimated_monthly_savings, recommendations.risk, recommendations.approvals_required)
                        is distinct from (excluded.estimated_monthly_savings, excluded.risk, excluded.approvals_required)
                   then recommendations.version + 1 else recommendations.version end,
    updated_at = now()
where recommendations.status in ('PROPOSED', 'PENDING_APPROVAL')
returning id, (xmax = 0) as created
"""


def upsert_finding(conn: Connection, org_id, *, scan_id, account_id, repo_id, resource_pk, finding: Finding, risk: str,
                   impact: str, priority: str, decision: PolicyDecision, explanation: str,
                   alternatives: list[dict[str, Any]], evidence: dict[str, Any], llm_advice: dict[str, Any] | None,
                   dedupe_key: str, actor: Actor = audit.SYSTEM) -> Upserted:
    status = sm.PENDING_APPROVAL if decision.allowed else sm.PROPOSED
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
        "dedupe_key": dedupe_key}).fetchone()
    if row is None:                       # existe en un estado posterior (aprobada, rechazada, PR...): no se toca
        return Upserted(None, False)
    if row["created"]:
        rec_id = row["id"]
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
                              "approvals_required": decision.approvals_required})
        return Upserted(rec_id, True)
    return Upserted(row["id"], False)


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
    return rec
