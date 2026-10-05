"""Orquestación de un escaneo: recolectar → normalizar → mapear IaC → reglas → riesgo/políticas → (LLM) → persistir.

Las llamadas de red (nube, Git, LLM) se hacen FUERA de las transacciones de base de datos.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

from psycopg.types.json import Jsonb

from .. import metrics
from ..collectors import get_collector
from ..config import Settings
from ..db import tenant_tx
from ..domain import policy as pol
from ..domain import risk as risk_mod
from ..domain.models import NormalizedResource
from ..domain.rules import Finding, RuleConfig, evaluate_all
from ..git.base import GitProviderError
from ..iac.patcher import PatchError, build_patch
from ..iac.terraform import IacIndex
from ..llm.advisor import advise
from ..secrets import SecretResolver
from . import audit
from .explain import build_explanation
from .git_factory import get_git_provider
from .recommendations import upsert_finding

log = logging.getLogger(__name__)


@dataclass
class _Prepared:
    finding: Finding
    risk: str
    impact: str
    priority: str
    decision: pol.PolicyDecision
    explanation: str
    alternatives: list[dict[str, Any]]
    evidence: dict[str, Any]
    llm_advice: dict[str, Any] | None


def _load_configs(conn) -> tuple[RuleConfig, pol.PolicyConfig]:
    rows = {r["kind"]: r["config"] for r in conn.execute(
        "select kind, config from policies where enabled order by created_at").fetchall()}
    rule_cfg = RuleConfig.from_overrides(rows.get("rule_config"))
    g = rows.get("guardrail") or {}
    base = pol.PolicyConfig()
    try:
        policy_cfg = pol.PolicyConfig(
            min_confidence_for_pr=float(g.get("min_confidence_for_pr", base.min_confidence_for_pr)),
            standard_approvals=max(1, int(g.get("standard_approvals", base.standard_approvals))),
            reinforced_approvals=max(2, int(g.get("reinforced_approvals", base.reinforced_approvals))))
    except (TypeError, ValueError):
        policy_cfg = base
    return rule_cfg, policy_cfg


def start_scan(org_id: UUID, scan_id: UUID) -> dict[str, Any]:
    """Marca RUNNING e incrementa intentos. Devuelve contexto para la fase de recolección."""
    with tenant_tx(org_id) as conn:
        scan = conn.execute("select * from scans where id = %s for update", (str(scan_id),)).fetchone()
        if scan is None:
            raise LookupError(f"scan {scan_id} no existe")
        if scan["status"] == "SUCCEEDED":
            return {"scan": scan, "skip": True}
        conn.execute("update scans set status = 'RUNNING', attempts = attempts + 1, "
                     "started_at = coalesce(started_at, now()), error = null where id = %s", (str(scan_id),))
        account = conn.execute("select * from cloud_accounts where id = %s", (str(scan["cloud_account_id"]),)).fetchone()
        repo = (conn.execute("select * from repositories where id = %s", (str(scan["repository_id"]),)).fetchone()
                if scan["repository_id"] else None)
        rule_cfg, policy_cfg = _load_configs(conn)
        hints = {r["resource_id"]: r["ts"] for r in conn.execute(
            """select resource_id, attributes ->> 'first_unattached_at' as ts from resources
                where cloud_account_id = %s and attached = false and attributes ? 'first_unattached_at'""",
            (str(account["id"]),)).fetchall()}
        audit.record(conn, org_id, audit.SCAN_STARTED, entity_type="scan", entity_id=scan_id,
                     payload={"attempt": scan["attempts"] + 1})
    return {"scan": scan, "account": account, "repo": repo, "rule_cfg": rule_cfg, "policy_cfg": policy_cfg,
            "unattached_hints": hints, "skip": False}


def _apply_unattached_tracking(resources: list[NormalizedResource], hints: dict[str, str | None]) -> None:
    """Volúmenes sin attachment: si CloudTrail no dio fecha, se usa la primera vez que el servicio los vio huérfanos."""
    now = datetime.now(timezone.utc)
    for r in resources:
        if r.service != "ebs" or r.attached is not False:
            continue
        if r.unattached_days is not None:
            r.attributes["first_unattached_at"] = (now - timedelta(days=r.unattached_days)).isoformat()
            continue
        first = hints.get(r.resource_id)
        try:
            first_dt = datetime.fromisoformat(first) if first else now
        except ValueError:
            first_dt = now
        r.attributes["first_unattached_at"] = first_dt.isoformat()
        r.unattached_days = max(0, (now - first_dt).days)


def prepare_findings(resources: list[NormalizedResource], *, rule_cfg: RuleConfig, policy_cfg: pol.PolicyConfig,
                     index: IacIndex | None, llm_client=None) -> list[_Prepared]:
    if index:
        for r in resources:
            block = index.match(r)
            if block:
                r.iac_address, r.iac_file = block.address, block.path
    prepared: list[_Prepared] = []
    for f in evaluate_all(resources, rule_cfg):
        risk = risk_mod.classify_risk(f)
        decision = pol.evaluate(action=f.action, risk=risk, environment=f.resource.environment,
                                confidence=f.confidence, cfg=policy_cfg)
        evidence = dict(f.evidence)
        if index and f.resource.iac_address:
            block = index.block_by_address(f.resource.iac_address)
            try:
                patch = build_patch(action=f.action, params=f.params, block=block, index=index)
                evidence["patch"] = {"path": patch.path, "diff": patch.diff, "summary": patch.summary,
                                     "validations": patch.validations}
            except PatchError as exc:
                evidence["patch_error"] = {"code": exc.code, "message": exc.message}
        elif index:
            evidence["patch_error"] = {"code": "iac_not_found", "message": "No se encontró el recurso en el IaC del repositorio"}
        explanation = build_explanation(f, risk, decision)
        alternatives = list(f.alternatives)
        llm_advice = None
        if llm_client is not None:
            result = advise(llm_client, f, risk, on_latency=metrics.LLM_LATENCY.observe)
            if result:
                explanation = f"{result.explanation}\n\n{explanation}"
                alternatives += result.alternatives
                llm_advice = {"notes": result.notes, "model_explanation": result.explanation}
        prepared.append(_Prepared(f, risk, risk_mod.financial_impact(f.estimated_monthly_savings),
                                  risk_mod.priority(f.estimated_monthly_savings, f.confidence, risk),
                                  decision, explanation, alternatives, evidence, llm_advice))
    return prepared


_RESOURCE_UPSERT = """
insert into resources (organization_id, cloud_account_id, last_scan_id, provider, resource_type, service, resource_id,
    region, name, environment, instance_type, volume_type, state, monthly_cost, cost_source, cpu_avg, cpu_max,
    memory_avg, attached, size_gb, age_days, observation_days, unattached_days, tags, iac_address, iac_file,
    attributes, active, last_seen_at)
values (%(org)s, %(account)s, %(scan)s, %(provider)s, %(resource_type)s, %(service)s, %(resource_id)s, %(region)s,
    %(name)s, %(environment)s, %(instance_type)s, %(volume_type)s, %(state)s, %(monthly_cost)s, %(cost_source)s,
    %(cpu_avg)s, %(cpu_max)s, %(memory_avg)s, %(attached)s, %(size_gb)s, %(age_days)s, %(observation_days)s,
    %(unattached_days)s, %(tags)s, %(iac_address)s, %(iac_file)s, %(attributes)s, true, now())
on conflict (organization_id, cloud_account_id, resource_id) do update set
    last_scan_id = excluded.last_scan_id, region = excluded.region, name = excluded.name,
    environment = excluded.environment, instance_type = excluded.instance_type, volume_type = excluded.volume_type,
    state = excluded.state, monthly_cost = excluded.monthly_cost, cost_source = excluded.cost_source,
    cpu_avg = excluded.cpu_avg, cpu_max = excluded.cpu_max, memory_avg = excluded.memory_avg,
    attached = excluded.attached, size_gb = excluded.size_gb, age_days = excluded.age_days,
    observation_days = excluded.observation_days, unattached_days = excluded.unattached_days, tags = excluded.tags,
    iac_address = excluded.iac_address, iac_file = excluded.iac_file, attributes = excluded.attributes,
    active = true, last_seen_at = now()
returning id
"""

_COST_UPSERT = """
insert into cost_records (organization_id, cloud_account_id, resource_pk, provider, usage_date, amount, service, region, source)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (organization_id, cloud_account_id, resource_pk, usage_date, service)
do update set amount = excluded.amount, source = excluded.source
"""


def run_scan(org_id: UUID, scan_id: UUID, *, settings: Settings, secrets: SecretResolver, llm_client=None,
             git_factory: Callable = get_git_provider, collector_factory: Callable = get_collector) -> dict[str, Any]:
    started = time.monotonic()
    ctx = start_scan(org_id, scan_id)
    if ctx["skip"]:
        return ctx["scan"]["stats"]
    account, repo = ctx["account"], ctx["repo"]
    warnings: list[str] = []

    # ---- 1) red: nube + IaC (sin transacción abierta)
    collector = collector_factory(
        account, secrets=secrets, demo_enabled=settings.demo_enabled,
        use_cost_explorer=settings.aws_cost_explorer_resources,
        on_api_error=lambda api: metrics.CLOUD_API_ERRORS.labels(account["provider"], api).inc())
    result = collector.collect()
    warnings += result.warnings

    index = None
    if repo:
        try:
            files = git_factory(repo, settings, secrets).list_files(repo["full_name"], repo["default_branch"], repo["iac_paths"])
            index = IacIndex.build(files)
            warnings += [f"IaC no analizable: {p}: {e}" for p, e in index.parse_errors.items()]
        except GitProviderError as exc:
            warnings.append(f"No se pudo leer el repositorio IaC: {exc.message}")

    # ---- 2) cómputo en memoria
    _apply_unattached_tracking(result.resources, ctx["unattached_hints"])
    prepared = prepare_findings(result.resources, rule_cfg=ctx["rule_cfg"], policy_cfg=ctx["policy_cfg"], index=index,
                                llm_client=llm_client)

    # ---- 3) persistencia
    created = updated = 0
    with tenant_tx(org_id) as conn:
        pk_by_rid: dict[str, str] = {}
        for r in result.resources:
            row = conn.execute(_RESOURCE_UPSERT, {
                "org": str(org_id), "account": str(account["id"]), "scan": str(scan_id), "provider": r.provider,
                "resource_type": r.resource_type, "service": r.service, "resource_id": r.resource_id,
                "region": r.region, "name": r.name, "environment": r.environment, "instance_type": r.instance_type,
                "volume_type": r.volume_type, "state": r.state, "monthly_cost": r.monthly_cost,
                "cost_source": r.cost_source, "cpu_avg": r.cpu_avg, "cpu_max": r.cpu_max, "memory_avg": r.memory_avg,
                "attached": r.attached, "size_gb": r.size_gb, "age_days": r.age_days,
                "observation_days": r.observation_days, "unattached_days": r.unattached_days,
                "tags": Jsonb(r.tags), "iac_address": r.iac_address, "iac_file": r.iac_file,
                "attributes": Jsonb(r.attributes)}).fetchone()
            pk_by_rid[r.resource_id] = str(row["id"])
        if not result.partial:
            conn.execute("update resources set active = false where cloud_account_id = %s and active "
                         "and last_scan_id is distinct from %s", (str(account["id"]), str(scan_id)))
        if result.costs:
            with conn.cursor() as cur:
                cur.executemany(_COST_UPSERT, [
                    (str(org_id), str(account["id"]), pk_by_rid[c.resource_id], account["provider"] if account["provider"] != "demo" else "aws",
                     c.usage_date, c.amount, c.service, c.region, c.source)
                    for c in result.costs if c.resource_id in pk_by_rid])
        for item in prepared:
            f = item.finding
            up = upsert_finding(
                conn, org_id, scan_id=scan_id, account_id=account["id"], repo_id=repo["id"] if repo else None,
                resource_pk=pk_by_rid[f.resource.resource_id], finding=f, risk=item.risk, impact=item.impact,
                priority=item.priority, decision=item.decision, explanation=item.explanation,
                alternatives=item.alternatives, evidence=item.evidence, llm_advice=item.llm_advice,
                dedupe_key=f.dedupe_key(str(account["id"])))
            if up.created:
                created += 1
                metrics.RECOMMENDATIONS_GENERATED.labels(f.rule_id).inc()
                metrics.ESTIMATED_SAVINGS_USD.inc(f.estimated_monthly_savings)
            elif up.rec_id:
                updated += 1
        stats = {"resources_seen": len(result.resources), "findings": len(prepared), "recommendations_created": created,
                 "recommendations_updated": updated, "partial": result.partial, "warnings": warnings[:20],
                 "estimated_monthly_savings": round(sum(i.finding.estimated_monthly_savings for i in prepared), 2),
                 "duration_seconds": round(time.monotonic() - started, 2)}
        conn.execute("update scans set status = 'SUCCEEDED', finished_at = now(), stats = %s where id = %s",
                     (Jsonb(stats), str(scan_id)))
        audit.record(conn, org_id, audit.SCAN_COMPLETED, entity_type="scan", entity_id=scan_id, payload=stats)
    metrics.SCANS_TOTAL.labels("succeeded").inc()
    return stats


def mark_scan_failed(org_id: UUID, scan_id: UUID, error: str, *, status: str = "FAILED") -> None:
    with tenant_tx(org_id) as conn:
        conn.execute("update scans set status = %s, finished_at = now(), error = %s where id = %s",
                     (status, error[:500], str(scan_id)))
        audit.record(conn, org_id, audit.SCAN_FAILED, entity_type="scan", entity_id=scan_id,
                     payload={"status": status, "error": error[:500]})
    metrics.SCANS_TOTAL.labels(status.lower()).inc()


def mark_scan_retrying(org_id: UUID, scan_id: UUID, error: str) -> None:
    with tenant_tx(org_id) as conn:
        conn.execute("update scans set status = 'QUEUED', error = %s where id = %s", (error[:500], str(scan_id)))
