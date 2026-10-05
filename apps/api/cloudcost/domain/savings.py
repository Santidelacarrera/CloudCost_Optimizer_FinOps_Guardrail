"""Medición de ahorro real: ahorro esperado vs. ahorro observado."""
from __future__ import annotations

from dataclasses import dataclass

from .pricing import DAYS_PER_MONTH


@dataclass(frozen=True)
class Realization:
    expected_monthly_savings: float
    baseline_monthly_cost: float
    observed_monthly_cost: float
    observed_monthly_savings: float
    realization_pct: float | None       # None si el ahorro esperado era 0


def monthly_from_window(total_cost: float, days: int) -> float:
    """Normaliza el costo acumulado en una ventana de `days` días a un mes promedio."""
    if days <= 0:
        raise ValueError("La ventana debe tener al menos 1 día")
    return round(total_cost / days * DAYS_PER_MONTH, 2)


def compute_realization(expected_monthly_savings: float, baseline_monthly_cost: float,
                        observed_monthly_cost: float) -> Realization:
    observed_savings = round(baseline_monthly_cost - observed_monthly_cost, 2)
    pct = round(observed_savings / expected_monthly_savings * 100, 1) if expected_monthly_savings > 0 else None
    return Realization(round(expected_monthly_savings, 2), round(baseline_monthly_cost, 2),
                       round(observed_monthly_cost, 2), observed_savings, pct)
