"""Evaluación PURA de presupuestos por tenant (sin base de datos ni red): fácil de probar y de auditar.

Niveles: ok < warn (consumo ≥ warn_ratio·límite) < forecast (la previsión de fin de mes supera el límite)
< exceeded (consumo + coste de la tarea > límite) y tope por tarea. El modo decide qué se hace al excederse:
alert (solo avisa), degrade (la tarea sigue con alcance reducido y sin gasto variable) o block (la tarea no arranca).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

MODES = ("alert", "degrade", "block")
METRICS = ("api_usage", "cloud_spend")


@dataclass(frozen=True)
class BudgetPolicy:
    metric: str
    monthly_limit_usd: float
    per_task_limit_usd: float | None = None
    warn_ratio: float = 0.8
    mode: str = "alert"
    enabled: bool = True


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool                         # la tarea puede ejecutarse tal como se pidió
    degrade: bool                         # puede ejecutarse solo con alcance reducido
    level: str                            # ok | warn | forecast | exceeded
    reasons: list[str] = field(default_factory=list)
    spent_usd: float = 0.0
    task_cost_usd: float = 0.0
    projected_usd: float = 0.0            # gasto + coste de la tarea
    forecast_month_end_usd: float | None = None
    limit_usd: float = 0.0
    remaining_usd: float = 0.0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _r(x: float, n: int = 2) -> float:
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))


def month_start(d: date) -> date:
    return d.replace(day=1)


def forecast_month_end(spent: float, today: date) -> float | None:
    """Previsión lineal del gasto al cierre del mes. Con menos de 3 días transcurridos no se extrapola (demasiado ruido)."""
    if today.day < 3:
        return None
    days = calendar.monthrange(today.year, today.month)[1]
    return _r(spent / today.day * days)


def evaluate(policy: BudgetPolicy | None, *, spent_usd: float, task_cost_usd: float = 0.0, today: date) -> BudgetDecision:
    """Decide si una tarea de coste `task_cost_usd` cabe en el presupuesto. Sin política activa: permitido."""
    if policy is None or not policy.enabled:
        return BudgetDecision(True, False, "ok", [], _r(spent_usd), _r(task_cost_usd), _r(spent_usd + task_cost_usd))
    if spent_usd < 0 or task_cost_usd < 0:
        raise ValueError("el gasto y el coste de la tarea no pueden ser negativos")
    limit = policy.monthly_limit_usd
    projected = spent_usd + task_cost_usd
    forecast = forecast_month_end(spent_usd, today)
    reasons: list[str] = []
    level = "ok"
    breach = False
    if policy.per_task_limit_usd is not None and task_cost_usd > policy.per_task_limit_usd:
        breach = True
        level = "exceeded"
        reasons.append(f"La tarea cuesta ~USD {_r(task_cost_usd, 4)} y supera el tope por tarea de USD {_r(policy.per_task_limit_usd, 4)}")
    if projected > limit:
        breach = True
        level = "exceeded"
        reasons.append(f"El gasto del mes (USD {_r(spent_usd)}) más la tarea (USD {_r(task_cost_usd, 4)}) supera el límite de USD {_r(limit)}")
    if level == "ok" and limit > 0 and projected >= policy.warn_ratio * limit:
        level = "warn"
        reasons.append(f"El consumo alcanza el {_r(projected / limit * 100, 1)} % del límite mensual (aviso al {_r(policy.warn_ratio * 100, 0)} %)")
    if level == "ok" and forecast is not None and forecast > limit:
        level = "forecast"
        reasons.append(f"A este ritmo el mes cerrará en ~USD {forecast}, por encima del límite de USD {_r(limit)}")
    allowed = not (breach and policy.mode in ("block", "degrade"))
    degrade = breach and policy.mode == "degrade"
    return BudgetDecision(allowed, degrade, level, reasons, _r(spent_usd), _r(task_cost_usd, 4), _r(projected), forecast, _r(limit),
                          _r(max(limit - projected, 0.0)))
