"""Proyección de ahorro: «Gasto actual» frente a «Gasto optimizado» mes a mes.

Modelo (deliberadamente simple y explícito, para poder explicarlo a quien aprueba):
  * Gasto actual   = gasto mensual inventariado hoy (costo de los recursos activos), constante hacia adelante. Es una línea base, no un
    pronóstico de consumo: no supone crecimiento ni estacionalidad.
  * Gasto optimizado = línea base menos el ahorro de las recomendaciones abiertas, que empieza a regir en el mes indicado en START_MONTH
    según su estado (cuanto más cerca de desplegarse, antes). Si el ahorro se verificó, se usa el observado.
  * «Esperado» = lo mismo pero cada ahorro multiplicado por su confianza (0-1): una cifra prudente.
  * Historial: costos reales de `cost_records` por mes cerrado; el mes en curso muestra la línea base. Un mes sin datos queda en blanco.
Las recomendaciones rechazadas o ya verificadas no suman ahorro nuevo (lo verificado ya forma parte del gasto real).
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:       # pragma: no cover
    from psycopg import Connection

# Primer mes futuro (1 = el mes que viene) en que el ahorro de una recomendación abierta empieza a notarse.
START_MONTH = {"DEPLOYED": 1, "MERGED": 1, "PR_CREATED": 1, "APPROVED": 1, "PENDING_APPROVAL": 2}
DEFAULT_BACK, DEFAULT_AHEAD, MAX_BACK, MAX_AHEAD = 3, 6, 12, 12
ASSUMPTIONS = [
    "El gasto actual es el costo mensual inventariado hoy y se mantiene constante (sin crecimiento ni estacionalidad).",
    "El ahorro de cada recomendación abierta empieza al mes siguiente si ya tiene PR, está aprobada o desplegada, y dos meses después si espera aprobación.",
    "«Esperado» pondera cada ahorro por su confianza; «Optimizado» supone que todas se aprueban y se aplican.",
    "El historial usa los costos importados o leídos de la nube; un mes sin datos queda en blanco.",
]


def add_months(d: date, n: int) -> date:
    """Primer día del mes que está `n` meses después (o antes, si n < 0) del mes de `d`."""
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def project(*, today: date, baseline: float, recs: Iterable[dict[str, Any]], history: dict[str, float],
            months_back: int = DEFAULT_BACK, months_ahead: int = DEFAULT_AHEAD) -> dict[str, Any]:
    """Función pura. `recs`: dicts con status, savings (USD/mes) y confidence (0-1). `history`: {"YYYY-MM": gasto del mes}."""
    months_back = max(0, min(months_back, MAX_BACK))
    months_ahead = max(1, min(months_ahead, MAX_AHEAD))
    current = today.replace(day=1)
    applicable = [(START_MONTH[r["status"]], float(r["savings"]), float(r["confidence"]))
                  for r in recs if r["status"] in START_MONTH and float(r["savings"]) > 0]
    potential = sum(s for _, s, _ in applicable)
    expected_total = sum(s * c for _, s, c in applicable)

    points: list[dict[str, Any]] = []
    for k in range(-months_back, 0):
        m = add_months(current, k).strftime("%Y-%m")
        points.append({"month": m, "actual": _r(history[m]) if m in history else None, "current": None, "optimized": None, "expected": None})
    points.append({"month": current.strftime("%Y-%m"), "actual": _r(baseline), "current": _r(baseline), "optimized": _r(baseline),
                   "expected": _r(baseline)})
    cum_opt = cum_exp = 0.0
    for k in range(1, months_ahead + 1):
        save = sum(s for start, s, _ in applicable if start <= k)
        save_exp = sum(s * c for start, s, c in applicable if start <= k)
        cum_opt, cum_exp = cum_opt + save, cum_exp + save_exp
        points.append({"month": add_months(current, k).strftime("%Y-%m"), "actual": None, "current": _r(baseline),
                       "optimized": _r(max(baseline - save, 0.0)), "expected": _r(max(baseline - save_exp, 0.0))})
    return {
        "currency": "USD",
        "as_of": today.isoformat(),
        "baseline_monthly": _r(baseline),
        "potential_monthly": _r(potential),
        "expected_monthly": _r(expected_total),
        "months_ahead": months_ahead,
        "cumulative_savings": {"optimized": _r(cum_opt), "expected": _r(cum_exp)},
        "history_months_with_data": sum(1 for p in points if p["actual"] is not None and p["current"] is None),
        "points": points,
        "assumptions": ASSUMPTIONS,
    }


def build(conn: "Connection", *, today: date | None = None, months_back: int = DEFAULT_BACK, months_ahead: int = DEFAULT_AHEAD) -> dict[str, Any]:
    today = today or date.today()
    months_back = max(0, min(months_back, MAX_BACK))
    baseline = float(conn.execute("select coalesce(sum(monthly_cost), 0) as v from resources where active").fetchone()["v"])
    statuses = list(START_MONTH)
    rows = conn.execute(
        """select r.status, r.confidence,
                  coalesce(case when r.status = 'DEPLOYED' then sv.observed_monthly_savings end, r.estimated_monthly_savings) as savings
             from recommendations r left join savings_verifications sv on sv.recommendation_id = r.id
            where r.status = any(%s)""", (statuses,)).fetchall()
    start = add_months(today.replace(day=1), -months_back)
    hist = conn.execute(
        """select to_char(date_trunc('month', usage_date), 'YYYY-MM') as m, sum(amount) as v from cost_records
            where usage_date >= %s and usage_date < %s group by 1""", (start, today.replace(day=1))).fetchall()
    return project(today=today, baseline=baseline, recs=rows, history={h["m"]: float(h["v"]) for h in hist},
                   months_back=months_back, months_ahead=months_ahead)
