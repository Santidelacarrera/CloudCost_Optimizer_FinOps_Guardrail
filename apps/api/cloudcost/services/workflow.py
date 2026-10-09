"""Human-in-the-loop: aprobación/rechazo, creación de PR, eventos de merge, despliegue y verificación de ahorro."""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta
from typing import Any, Callable

import psycopg.errors
from psycopg import Connection
from psycopg.types.json import Jsonb

from .. import guardrail, metrics
from ..config import Settings
from ..domain import policy as pol
from ..domain import state_machine as sm
from ..domain.models import NormalizedResource
from ..domain.savings import compute_realization, monthly_from_window
from ..git import pr_body
from ..git.base import GitProvider, GitProviderError
from ..iac.patcher import PatchError, build_patch
from ..iac.terraform import IacIndex
from ..secrets import SecretResolver
from ..security import Principal
from . import audit
from .git_factory import get_git_provider
from .recommendations import WorkflowError, get_detail, transition

MIN_VERIFICATION_DAYS = 7


def ensure_user(conn: Connection, p: Principal):
    return conn.execute(
        """insert into users (organization_id, external_id, email, role) values (%s, %s, %s, %s)
           on conflict (organization_id, external_id) do update set email = excluded.email, role = excluded.role
           returning id""",
        (str(p.org_id), p.user_id, p.email, p.role)).fetchone()["id"]


def _lock(conn: Connection, rec_id, *, nowait: bool = False) -> dict[str, Any]:
    try:
        rec = conn.execute(f"select * from recommendations where id = %s for update{' nowait' if nowait else ''}",
                           (str(rec_id),)).fetchone()
    except psycopg.errors.LockNotAvailable as exc:
        raise WorkflowError(409, "Otra operación está procesando esta recomendación; inténtalo de nuevo", "locked") from exc
    if rec is None:
        raise WorkflowError(404, "Recomendación no encontrada", "not_found")
    return rec


# ---------------------------------------------------------------------------- aprobación
def decide(conn: Connection, p: Principal, rec_id, decision: str, reason: str, expected_version: int | None) -> dict[str, Any]:
    assert decision in ("APPROVED", "REJECTED")
    if not pol.can_approve(p.role):
        raise WorkflowError(403, f"El rol {p.role} no puede aprobar ni rechazar", "forbidden")
    rec = _lock(conn, rec_id)
    if expected_version is not None and rec["version"] != expected_version:
        raise WorkflowError(409, "La recomendación cambió desde que la abriste; revisa los nuevos datos", "stale_version")
    policy_data = rec["policy"] or {}
    if decision == "APPROVED":
        if rec["status"] != sm.PENDING_APPROVAL:
            raise WorkflowError(409, f"No se puede aprobar una recomendación en estado {rec['status']}", "invalid_state")
        if not policy_data.get("allowed", False):
            raise WorkflowError(409, "Bloqueada por política: " + "; ".join(policy_data.get("blocked_reasons", [])), "policy_blocked")
    elif rec["status"] not in (sm.PENDING_APPROVAL, sm.APPROVED):
        raise WorkflowError(409, f"No se puede rechazar una recomendación en estado {rec['status']}", "invalid_state")

    user_id = ensure_user(conn, p)
    context = {"estimated_monthly_savings": float(rec["estimated_monthly_savings"]), "risk": rec["risk"],
               "confidence": float(rec["confidence"]), "status": rec["status"], "approvals_required": rec["approvals_required"]}
    try:
        conn.execute(
            """insert into approvals (organization_id, recommendation_id, user_id, approver_role, decision, reason,
                                      recommendation_version, context) values (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(p.org_id), str(rec_id), str(user_id), p.role, decision, reason, rec["version"], Jsonb(context)))
    except psycopg.errors.UniqueViolation as exc:
        raise WorkflowError(409, "Ya registraste una decisión para esta recomendación", "already_decided") from exc

    actor = audit.user_actor(p)
    if decision == "REJECTED":
        transition(conn, p.org_id, rec_id, rec["status"], sm.REJECTED, actor, reason)
        audit.record(conn, p.org_id, audit.APPROVAL_REJECTED, actor=actor, entity_type="recommendation", entity_id=rec_id,
                     payload={"reason": reason, "version": rec["version"], "role": p.role, **context})
        metrics.RECOMMENDATIONS_REJECTED.inc()
        return {"status": sm.REJECTED, "approvals": 0, "approvals_required": rec["approvals_required"]}

    roles = [r["approver_role"] for r in conn.execute(
        """select approver_role from approvals
            where recommendation_id = %s and decision = 'APPROVED' and recommendation_version = %s""",
        (str(rec_id), rec["version"])).fetchall()]
    final = pol.approval_satisfied(roles, pol.PolicyDecision(**policy_data))
    audit.record(conn, p.org_id, audit.APPROVAL_GRANTED, actor=actor, entity_type="recommendation", entity_id=rec_id,
                 payload={"reason": reason, "version": rec["version"], "role": p.role, "approvals": len(roles),
                          "required": rec["approvals_required"], "final": final, **context})
    if final:
        transition(conn, p.org_id, rec_id, sm.PENDING_APPROVAL, sm.APPROVED, actor, "Aprobaciones requeridas completas")
        metrics.RECOMMENDATIONS_APPROVED.inc()
    return {"status": sm.APPROVED if final else sm.PENDING_APPROVAL, "approvals": len(roles),
            "approvals_required": rec["approvals_required"]}


# ---------------------------------------------------------------------------- Pull Request
def _resource_for_matching(row: dict[str, Any]) -> NormalizedResource:
    return NormalizedResource(provider=row["provider"], resource_type=row["resource_type"], service=row["service"],
                              resource_id=row["resource_id"], region=row["region"], name=row["name"],
                              tags=row["tags"] or {}, iac_address=row["iac_address"], attributes=row["attributes"] or {})


def create_pull_request(conn: Connection, p: Principal, rec_id, *, settings: Settings, secrets: SecretResolver,
                        provider_factory: Callable[..., GitProvider] = get_git_provider) -> dict[str, Any]:
    rec = _lock(conn, rec_id, nowait=True)
    existing = conn.execute("select * from pull_requests where recommendation_id = %s", (str(rec_id),)).fetchone()
    if existing:
        return {"created": False, **{k: existing[k] for k in ("number", "url", "branch", "draft", "state")}}
    if rec["status"] != sm.APPROVED:
        raise WorkflowError(409, f"Solo se puede crear el PR de una recomendación APPROVED (estado actual: {rec['status']})", "invalid_state")
    if not (rec["policy"] or {}).get("allowed", False):
        raise WorkflowError(409, "Bloqueada por política", "policy_blocked")
    if not rec["repository_id"]:
        raise WorkflowError(422, "El escaneo no tenía un repositorio IaC asociado", "no_repository")
    repo = conn.execute("select * from repositories where id = %s", (str(rec["repository_id"]),)).fetchone()
    res = conn.execute("select * from resources where id = %s", (str(rec["resource_pk"]),)).fetchone()

    try:
        provider = provider_factory(repo, settings, secrets)
        files = provider.list_files(repo["full_name"], repo["default_branch"], repo["iac_paths"])
    except GitProviderError as exc:
        raise WorkflowError(502, f"No se pudo leer el repositorio: {exc.message}", "git_error") from exc
    index = IacIndex.build(files)
    block = (index.block_by_address(res["iac_address"]) if res["iac_address"] else None) or index.match(_resource_for_matching(res))
    if block is None:
        why = index.why_no_match(_resource_for_matching(res))
        raise WorkflowError(422, "No se encontró el recurso en el IaC del repositorio (¿etiqueta Name o estado desactualizados?)"
                            if why["code"] == "iac_not_found" else why["message"], why["code"])
    try:
        patch = build_patch(action=rec["action"], params=rec["params"], block=block, index=index)
    except PatchError as exc:
        raise WorkflowError(422, exc.message, f"patch_{exc.code}") from exc

    approvals = conn.execute(
        """select a.approver_role, a.reason, a.created_at, a.user_id, u.email from approvals a join users u on u.id = a.user_id
            where a.recommendation_id = %s and a.decision = 'APPROVED' and a.recommendation_version = %s order by a.created_at""",
        (str(rec_id), rec["version"])).fetchall()
    # Política (OPA/Rego) evaluada AQUÍ, antes de tocar el repositorio: no depende de que el CI del cliente la ejecute.
    verdict = guardrail.evaluate_plan(settings, guardrail.plan_from_change(
        tf_type=block.type, address=block.address, action=rec["action"], params=rec["params"], resource=res, rec=rec,
        approvals=approvals))
    if verdict.mode != "off":
        audit.record(conn, p.org_id, audit.POLICY_EVALUATED, actor=audit.user_actor(p), entity_type="recommendation",
                     entity_id=rec_id, payload=verdict.audit_payload())
    if verdict.blocked:
        return {"created": False, "blocked": True, "code": "policy_engine_unavailable" if verdict.error else "policy_denied",
                "violations": verdict.violations, "error": verdict.error}

    draft = bool((rec["policy"] or {}).get("pr_as_draft"))
    branch = pr_body.branch_name(rec)
    body = pr_body.pr_body(rec, patch_summary=patch.summary, validations=patch.validations, approvals=approvals,
                           dashboard_url=f"{settings.public_web_url}/recommendations/{rec['id']}",
                           policy_notes=_policy_notes(verdict))
    try:
        cr = provider.create_change_request(
            repo=repo["full_name"], base_branch=repo["default_branch"], branch=branch, path=patch.path,
            new_text=patch.new_text, commit_message=pr_body.commit_message(rec), title=pr_body.pr_title(rec), body=body,
            draft=draft, labels=["finops", "automated"] + (["do-not-auto-merge"] if rec["automation_blocked"] else []))
    except GitProviderError as exc:
        raise WorkflowError(502, f"Error del proveedor Git: {exc.message}", "git_error") from exc

    conn.execute(
        """insert into pull_requests (organization_id, recommendation_id, repository_id, provider, number, url, branch,
                                      base_branch, draft, diff, validations, created_by)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (str(p.org_id), str(rec_id), str(repo["id"]), repo["provider"], cr.number, cr.url, cr.branch,
         repo["default_branch"], cr.draft, patch.diff, Jsonb(patch.validations), p.user_id))
    actor = audit.user_actor(p)
    transition(conn, p.org_id, rec_id, sm.APPROVED, sm.PR_CREATED, actor, f"PR #{cr.number}")
    audit.record(conn, p.org_id, audit.PR_CREATED, actor=actor, entity_type="recommendation", entity_id=rec_id,
                 payload={"number": cr.number, "url": cr.url, "branch": cr.branch, "draft": cr.draft,
                          "repository": repo["full_name"], "summary": patch.summary})
    return {"created": True, "number": cr.number, "url": cr.url, "branch": cr.branch, "draft": cr.draft, "state": "open",
            "diff": patch.diff}


def _policy_notes(verdict: guardrail.GuardrailVerdict) -> list[str]:
    if verdict.mode == "off":
        return []
    if verdict.error:
        return [f"⚠️ No se pudo evaluar la política OPA ({verdict.error}); modo {verdict.mode}."]
    if verdict.violations:
        return [f"⚠️ Violaciones de política (modo {verdict.mode}, no bloquearon el PR):"] + [f"  - {v}" for v in verdict.violations]
    return [f"✅ Política OPA evaluada sin violaciones (políticas {verdict.policy_digest})."]


# ---------------------------------------------------------------------------- eventos externos
def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handle_pull_request_event(conn: Connection, org_id, payload: dict[str, Any], *, provider: str = "github") -> dict[str, Any]:
    if payload.get("action") != "closed":
        return {"handled": False, "reason": "ignored_action"}
    pr = payload.get("pull_request") or {}
    full_name = (payload.get("repository") or {}).get("full_name")
    row = conn.execute(
        """select p.id, p.recommendation_id from pull_requests p join repositories r on r.id = p.repository_id
            where r.full_name = %s and r.provider = %s and p.number = %s""",
        (full_name, provider, pr.get("number"))).fetchone()
    if not row:
        return {"handled": False, "reason": "unknown_pull_request"}
    rec_id = row["recommendation_id"]
    rec = _lock(conn, rec_id)
    if pr.get("merged"):
        conn.execute("update pull_requests set state = 'merged', merged_at = now() where id = %s", (str(row["id"]),))
        if rec["status"] == sm.PR_CREATED:
            transition(conn, org_id, rec_id, sm.PR_CREATED, sm.MERGED, audit.WEBHOOK, f"PR #{pr.get('number')} fusionado")
            audit.record(conn, org_id, audit.PR_MERGED, actor=audit.WEBHOOK, entity_type="recommendation", entity_id=rec_id,
                         payload={"number": pr.get("number"), "merged_by": (pr.get("merged_by") or {}).get("login")})
        return {"handled": True, "state": "merged"}
    conn.execute("update pull_requests set state = 'closed' where id = %s", (str(row["id"]),))
    if rec["status"] == sm.PR_CREATED:
        transition(conn, org_id, rec_id, sm.PR_CREATED, sm.APPROVED, audit.WEBHOOK, "PR cerrado sin fusionar")
        audit.record(conn, org_id, audit.PR_CLOSED, actor=audit.WEBHOOK, entity_type="recommendation", entity_id=rec_id,
                     payload={"number": pr.get("number")})
    return {"handled": True, "state": "closed"}


def mark_merged_local(conn: Connection, p: Principal, rec_id, settings: Settings) -> dict[str, Any]:
    """Solo modo demo: el proveedor 'local' no tiene webhooks, así que el merge se simula manualmente."""
    rec = _lock(conn, rec_id)
    pr = conn.execute("select p.*, r.provider as repo_provider from pull_requests p join repositories r on r.id = p.repository_id "
                      "where p.recommendation_id = %s", (str(rec_id),)).fetchone()
    if not settings.demo_enabled or not pr or pr["repo_provider"] != "local":
        raise WorkflowError(409, "Solo disponible para PR del repositorio demo (provider 'local')", "not_local")
    conn.execute("update pull_requests set state = 'merged', merged_at = now() where id = %s", (str(pr["id"]),))
    transition(conn, p.org_id, rec_id, rec["status"], sm.MERGED, audit.user_actor(p), "Merge simulado (demo)")
    audit.record(conn, p.org_id, audit.PR_MERGED, actor=audit.user_actor(p), entity_type="recommendation", entity_id=rec_id,
                 payload={"simulated": True})
    return {"status": sm.MERGED}


def mark_deployed(conn: Connection, p: Principal, rec_id, *, reference: str | None) -> dict[str, Any]:
    rec = _lock(conn, rec_id)
    actor = audit.user_actor(p)
    transition(conn, p.org_id, rec_id, rec["status"], sm.DEPLOYED, actor, reference, set_sql="deployed_at = now()")
    audit.record(conn, p.org_id, audit.DEPLOYMENT, actor=actor, entity_type="recommendation", entity_id=rec_id,
                 payload={"reference": reference})
    return {"status": sm.DEPLOYED}


# ---------------------------------------------------------------------------- ahorro real
def verify_savings(conn: Connection, p: Principal, rec_id, *, observed_monthly_cost: float | None) -> dict[str, Any]:
    rec = _lock(conn, rec_id)
    if rec["status"] != sm.DEPLOYED:
        raise WorkflowError(409, f"Solo se verifica el ahorro tras el despliegue (estado actual: {rec['status']})", "invalid_state")
    baseline = float(rec["current_monthly_cost"])
    window_start = window_end = None
    if observed_monthly_cost is None:
        window_start = rec["deployed_at"].date() + timedelta(days=1)
        window_end = date.today()
        days = (window_end - window_start).days
        if days < MIN_VERIFICATION_DAYS:
            raise WorkflowError(422, f"Se necesitan al menos {MIN_VERIFICATION_DAYS} días de costos posteriores al despliegue (hay {max(days, 0)})",
                                "insufficient_data")
        has_data = conn.execute("select 1 from cost_records where cloud_account_id = %s and usage_date >= %s limit 1",
                                (str(rec["cloud_account_id"]), window_start)).fetchone()
        if not has_data:
            raise WorkflowError(422, "No hay registros de costos posteriores al despliegue para esta cuenta", "insufficient_data")
        total = conn.execute(
            """select coalesce(sum(amount), 0) as total from cost_records
                where resource_pk = %s and usage_date >= %s and usage_date < %s""",
            (str(rec["resource_pk"]), window_start, window_end)).fetchone()["total"]
        observed = monthly_from_window(float(total), days)
        method = "cost_records"
    else:
        observed, method = round(float(observed_monthly_cost), 2), "manual"
    real = compute_realization(float(rec["estimated_monthly_savings"]), baseline, observed)
    conn.execute(
        """insert into savings_verifications (organization_id, recommendation_id, expected_monthly_savings,
               baseline_monthly_cost, observed_monthly_cost, observed_monthly_savings, realization_pct, window_start,
               window_end, method, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (str(p.org_id), str(rec_id), real.expected_monthly_savings, real.baseline_monthly_cost, real.observed_monthly_cost,
         real.observed_monthly_savings, real.realization_pct, window_start, window_end, method, p.user_id))
    actor = audit.user_actor(p)
    transition(conn, p.org_id, rec_id, sm.DEPLOYED, sm.VERIFIED, actor, f"Realización {real.realization_pct}%",
               set_sql="verified_at = now()")
    audit.record(conn, p.org_id, audit.SAVINGS_VERIFIED, actor=actor, entity_type="recommendation", entity_id=rec_id,
                 payload={"method": method, **real.__dict__})
    if real.observed_monthly_savings > 0:
        metrics.REALIZED_SAVINGS_USD.inc(real.observed_monthly_savings)
    return {"status": sm.VERIFIED, **real.__dict__, "method": method}


def recommendation_detail(conn: Connection, rec_id) -> dict[str, Any]:
    return get_detail(conn, rec_id)
