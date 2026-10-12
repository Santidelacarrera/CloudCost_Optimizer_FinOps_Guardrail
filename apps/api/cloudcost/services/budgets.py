"""Guardrail de presupuesto por tenant: lectura de políticas, medición de uso, evaluación previa y alertas auditadas.

Todo corre dentro de una transacción de tenant (RLS): una organización jamás ve ni afecta los presupuestos de otra.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from psycopg import Connection

from ..domain.budgets import BudgetDecision, BudgetPolicy, evaluate, month_start
from . import audit

BUDGET_EVALUATED = "BUDGET_EVALUATED"
BUDGET_UPDATED = "BUDGET_UPDATED"
BUDGET_ALERT = "BUDGET_ALERT"
CE_REQUEST_USD = 0.01            # precio de lista de cada solicitud a Cost Explorer


class BudgetExceeded(PermissionError):
    """Tarea bloqueada por presupuesto. Hereda de PermissionError: el worker no la reintenta."""


def today_utc() -> date:
    return datetime.now(UTC).date()


def get_policy(conn: Connection, metric: str) -> BudgetPolicy | None:
    row = conn.execute("select * from tenant_budgets where metric = %s", (metric,)).fetchone()
    if not row:
        return None
    return BudgetPolicy(metric=row["metric"], monthly_limit_usd=float(row["monthly_limit_usd"]),
                        per_task_limit_usd=float(row["per_task_limit_usd"]) if row["per_task_limit_usd"] is not None else None,
                        warn_ratio=float(row["warn_ratio"]), mode=row["mode"], enabled=row["enabled"])


def spent_this_month(conn: Connection, metric: str, today: date) -> float:
    start = month_start(today)
    if metric == "api_usage":
        row = conn.execute("select coalesce(sum(cost_usd), 0) as s from usage_meter where period = %s", (start,)).fetchone()
    else:   # cloud_spend: gasto observado del mes (USD) en costes de cuenta
        row = conn.execute(
            "select coalesce(sum(amount), 0) as s from account_costs where currency = 'USD' and granularity = 'DAILY' "
            "and period_start >= %s and period_start <= %s and amount > 0", (start, today)).fetchone()
        if not float(row["s"]):
            row = conn.execute(
                "select coalesce(sum(amount), 0) as s from account_costs where currency = 'USD' and granularity = 'MONTHLY' "
                "and period_start = %s and amount > 0", (start,)).fetchone()
    return float(row["s"])


def _alert(conn: Connection, org_id: UUID, metric: str, level: str, decision: BudgetDecision, today: date) -> None:
    msg = "; ".join(decision.reasons) or level
    inserted = conn.execute(
        """insert into budget_alerts (organization_id, metric, level, period, message, details) values (%s, %s, %s, %s, %s, %s)
           on conflict (organization_id, metric, level, period) do nothing returning id""",
        (str(org_id), metric, level, month_start(today), msg[:500], _jsonb(decision.to_dict()))).fetchone()
    if inserted:                                                    # una alerta por nivel y período: sin spam, pero siempre auditada
        audit.record(conn, org_id, BUDGET_ALERT, entity_type="budget", entity_id=None,
                     payload={"metric": metric, "level": level, "message": msg[:300]})


def _jsonb(value: Any):
    from psycopg.types.json import Jsonb
    return Jsonb(value)


def preflight(conn: Connection, org_id: UUID, metric: str, task_cost_usd: float, *, today: date | None = None,
              record_alerts: bool = True) -> BudgetDecision:
    """Evalúa si una tarea cabe en el presupuesto y registra la alerta correspondiente. NO bloquea por sí misma: devuelve la decisión."""
    today = today or today_utc()
    policy = get_policy(conn, metric)
    decision = evaluate(policy, spent_usd=spent_this_month(conn, metric, today), task_cost_usd=task_cost_usd, today=today)
    if record_alerts and policy and policy.enabled and decision.level != "ok":
        level = decision.level
        if level == "exceeded" and metric == "api_usage":                # cloud_spend nunca bloquea ni degrada: su alerta es siempre «exceeded»
            level = "degraded" if decision.degrade else ("blocked" if not decision.allowed else "exceeded")
        _alert(conn, org_id, metric, level, decision, today)
    return decision


def record_usage(conn: Connection, org_id: UUID, source: str, cost_usd: float, *, quantity: float = 1.0, ref: str | None = None,
                 today: date | None = None) -> None:
    if cost_usd < 0 or quantity < 0:
        raise ValueError("el uso medido no puede ser negativo")
    conn.execute("insert into usage_meter (organization_id, source, ref, quantity, cost_usd, period) values (%s, %s, %s, %s, %s, %s)",
                 (str(org_id), source[:60], ref, quantity, round(cost_usd, 4), month_start(today or today_utc())))


def status(conn: Connection, today: date | None = None) -> list[dict]:
    today = today or today_utc()
    out = []
    for row in conn.execute("select metric from tenant_budgets order by metric").fetchall():
        policy = get_policy(conn, row["metric"])
        d = evaluate(policy, spent_usd=spent_this_month(conn, row["metric"], today), today=today)
        out.append({"metric": row["metric"], "mode": policy.mode, "enabled": policy.enabled, "warn_ratio": policy.warn_ratio,
                    "per_task_limit_usd": policy.per_task_limit_usd, **d.to_dict()})
    return out


def scan_cost_estimate(account: dict, settings) -> float:
    """Coste máximo variable de un escaneo: solo AWS con Cost Explorer genera cargos (USD 0,01 por solicitud)."""
    if account.get("provider") == "aws" and settings.aws_cost_explorer_resources:
        return round(settings.aws_ce_request_budget * CE_REQUEST_USD, 4)
    return 0.0
