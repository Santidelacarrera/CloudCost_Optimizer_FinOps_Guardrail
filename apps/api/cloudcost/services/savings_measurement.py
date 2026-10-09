"""Línea base y medición del ahorro observado (capa de base de datos sobre domain/measurement.py).

Dos momentos fijan «el coste previo»:
  · al COMPLETARSE la aprobación: se guarda el coste de los 14 días anteriores y la utilización de ese instante (lo que vio el aprobador);
  · al marcar el DESPLIEGUE: se vuelve a calcular con la ventana que termina el día del despliegue (puede haber pasado tiempo desde la
    aprobación). Es la que se usa para medir; la de la aprobación es el respaldo si faltan datos.
Las líneas base son append-only: lo registrado no se reescribe.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from ..domain import measurement as ms
from ..domain.pricing import DAYS_PER_MONTH
from . import audit
from .evidence import evidence_hash

# Servicio del recurso -> servicio normalizado de `account_costs` que lo contiene.
ACCOUNT_SERVICE = {"ec2": ("ec2",), "ebs": ("ec2_other",), "ebs_snapshot": ("ec2_other",), "rds": ("rds",)}


def resource_series(conn: Connection, resource_pk, start: date, end: date) -> tuple[dict[date, float], dict[date, str]]:
    rows = conn.execute(
        """select usage_date, sum(amount) as amount, min(source) as source from cost_records
            where resource_pk = %s and usage_date >= %s and usage_date < %s group by usage_date""",
        (str(resource_pk), start, end)).fetchall()
    return {r["usage_date"]: float(r["amount"]) for r in rows}, {r["usage_date"]: r["source"] for r in rows}


def billing_days(conn: Connection, account_id, start: date, end: date) -> set[date]:
    """Días con facturación REAL de la cuenta (de cualquier recurso): prueba de que un día sin fila del recurso es un cero y no un hueco."""
    rows = conn.execute(
        """select distinct usage_date from cost_records
            where cloud_account_id = %s and usage_date >= %s and usage_date < %s and source = any(%s)""",
        (str(account_id), start, end, list(ms.BILLING_SOURCES))).fetchall()
    return {r["usage_date"] for r in rows}


def _service_daily(conn: Connection, account_id, services: tuple[str, ...], start: date, end: date) -> dict[date, float]:
    rows = conn.execute(
        """select period_start, sum(amount) as amount from account_costs
            where cloud_account_id = %s and granularity = 'DAILY' and currency = 'USD' and service = any(%s)
              and period_start >= %s and period_start < %s group by period_start""",
        (str(account_id), list(services), start, end)).fetchall()
    return {r["period_start"]: float(r["amount"]) for r in rows}


def peers(conn: Connection, account_id, service: str, own: dict[date, float], windows: ms.Windows) -> tuple[list[float] | None, list[float] | None]:
    """Coste diario del RESTO de la cuenta en el mismo servicio (sin este recurso) en las dos ventanas, o (None, None) si no hay cobertura."""
    services = ACCOUNT_SERVICE.get(service)
    if not services:
        return None, None
    out = []
    for start, end in ((windows.pre_start, windows.pre_end), (windows.post_start, windows.post_end)):
        total = _service_daily(conn, account_id, services, start, end)
        if len(total) < ms.MIN_COVERAGE * (end - start).days:
            return None, None
        out.append([max(0.0, total[d] - own.get(d, 0.0)) for d in sorted(total)])
    return out[0], out[1]


def usage_snapshot(res: dict[str, Any]) -> dict[str, Any]:
    return {k: (float(res[k]) if k in ("cpu_avg", "cpu_max", "memory_avg") and res.get(k) is not None else res.get(k))
            for k in ("instance_type", "state", "cpu_avg", "cpu_max", "memory_avg", "attached", "active", "environment", "cost_source")}


def capture_baseline(conn: Connection, org_id, rec: dict[str, Any], res: dict[str, Any], *, phase: str, as_of: date,
                     created_by: str, actor: audit.Actor) -> dict[str, Any]:
    """Registra el coste de los 14 días previos a `as_of` (exclusivo) con su procedencia y la utilización del momento."""
    start = as_of - timedelta(days=ms.PRE_DAYS)
    daily, sources = resource_series(conn, rec["resource_pk"], start, as_of)
    billing = [d for d, s in sources.items() if s in ms.BILLING_SOURCES]
    if daily:
        monthly = sum(daily.values()) / len(daily) * DAYS_PER_MONTH
        grade = "billing" if len(billing) / len(daily) >= 0.8 else "model"
        source = "cost_explorer" if grade == "billing" and any(sources[d] == "cost_explorer" for d in billing) else \
            "import" if grade == "billing" else "estimate"
    else:                                                   # sin series: lo único que hay es la estimación vigente
        monthly, grade, source = float(rec["current_monthly_cost"]), "model", "estimate_only"
    series = [[d.isoformat(), round(a, 4)] for d, a in sorted(daily.items())]
    usage = usage_snapshot(res)
    digest = evidence_hash({"rec": str(rec["id"]), "phase": phase, "window": [start, as_of], "daily": series, "usage": usage,
                            "monthly": round(monthly, 2), "source": source})
    row = conn.execute(
        """insert into savings_baselines (organization_id, recommendation_id, phase, window_start, window_end, days_expected, days_with_data,
               daily_costs, total_cost, monthly_cost, cost_source, data_grade, usage, baseline_hash, created_by)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (str(org_id), str(rec["id"]), phase, start, as_of, ms.PRE_DAYS, len(daily), Jsonb(series), round(sum(daily.values()), 4),
         round(monthly, 2), source, grade, Jsonb(usage), digest, created_by)).fetchone()
    audit.record(conn, org_id, audit.BASELINE_CAPTURED, actor=actor, entity_type="recommendation", entity_id=rec["id"],
                 payload={"phase": phase, "window": [start.isoformat(), as_of.isoformat()], "days_with_data": len(daily),
                          "monthly_cost": round(monthly, 2), "cost_source": source, "data_grade": grade, "baseline_hash": digest})
    return row


def latest_baseline(conn: Connection, rec_id, phase: str | None = None) -> dict[str, Any] | None:
    return conn.execute(
        """select * from savings_baselines where recommendation_id = %s and (%s::text is null or phase = %s)
            order by (phase = 'deployment') desc, created_at desc limit 1""", (str(rec_id), phase, phase)).fetchone()


def measure_recommendation(conn: Connection, rec: dict[str, Any], res: dict[str, Any], *, deployed_on: date, today: date) -> tuple[ms.Measurement, ms.Windows, dict | None]:
    """Mide un cambio desplegado. Lanza `InsufficientData` si aún no hay días completos suficientes."""
    windows = ms.plan_windows(deployed_on, today)
    daily, sources = resource_series(conn, rec["resource_pk"], windows.pre_start, windows.post_end)
    stored = latest_baseline(conn, rec["id"])
    approved = float(rec["approved_monthly_savings"] if rec.get("approved_monthly_savings") is not None else rec["estimated_monthly_savings"])
    fallback = float(rec["approved_baseline_cost"] if rec.get("approved_baseline_cost") is not None else rec["current_monthly_cost"])
    pre_usage = (stored or {}).get("usage") or None
    pre_peers, post_peers = peers(conn, rec["cloud_account_id"], res["service"], daily, windows)
    m = ms.measure(
        action=rec["action"], params=rec["params"] or {}, windows=windows, approved_savings=approved, baseline_fallback_monthly=fallback,
        resource_daily=daily, sources=sources, billing_days=billing_days(conn, rec["cloud_account_id"], windows.post_start, windows.post_end),
        resource_exists_now=bool(res["active"]) if res else None, usage_pre=pre_usage, usage_post=usage_snapshot(res) if res else None,
        peers_pre=pre_peers, peers_post=post_peers,
        baseline_monthly_stored=float(stored["monthly_cost"]) if stored else None, baseline_grade_stored=stored["data_grade"] if stored else None)
    return m, windows, stored
