"""Costos REALES de AWS vía Cost Explorer (solo lectura): por recurso (ARN/ID) y por etiqueta de asignación de costos.

Dos fuentes complementarias, porque AWS no da más con una sola llamada:

* `GetCostAndUsageWithResources` — costo DIARIO por recurso. AWS lo limita a los últimos 14 días. Es el dato que alimenta
  `current_monthly_cost` (el costo con el que se calcula el ahorro) antes de proponer apagar o reducir algo.
* `GetCostAndUsage` agrupado por TAG — costo MENSUAL por valor de etiqueta, hasta 12 meses. Da el historial de largo plazo
  (tendencia) para etiquetas de asignación de costos activadas en la cuenta de pagos. El costo de una etiqueta agrupa a
  todos los recursos que la comparten: se informa como tal (`scope: "tag"`), nunca se reparte a un recurso individual.

Todas las llamadas son lecturas. Un fallo de Cost Explorer NO invalida el inventario: se degrada a la tabla de precios y se avisa.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

RESOURCE_WINDOW_DAYS = 14               # límite de AWS para costos por recurso
MAX_HISTORY_MONTHS = 12                 # límite de AWS para granularidad mensual
SERVICES_EC2 = ("Amazon Elastic Compute Cloud - Compute", "EC2 - Other")   # EC2 - Other incluye EBS y snapshots
COST_METRICS = ("UnblendedCost", "AmortizedCost", "NetUnblendedCost", "NetAmortizedCost")
DEFAULT_METRIC = "UnblendedCost"


def normalize_resource_id(raw: str) -> str:
    """`arn:aws:ec2:us-east-1:123:instance/i-0abc` -> `i-0abc`. Los IDs que ya son simples se devuelven igual."""
    value = (raw or "").strip()
    if value.startswith("arn:"):
        tail = value.split(":", 5)[-1]            # recurso: `instance/i-0abc` o `volume/vol-1`
        return tail.rsplit("/", 1)[-1]
    return value


@dataclass
class ResourceCost:
    daily: list[tuple[date, float]] = field(default_factory=list)

    @property
    def days_with_data(self) -> int:
        return len({d for d, _ in self.daily})

    def monthly_estimate(self, days_in_month: float, *, window_days: int = RESOURCE_WINDOW_DAYS,
                         age_days: int | None = None) -> float:
        """Costo mensual = promedio diario de los días con costo × días del mes.

        Si el recurso es más joven que la ventana, se promedia sobre su edad (no sobre días sin existir).
        """
        if not self.daily:
            return 0.0
        total = sum(a for _, a in self.daily)
        days = max(1, min(window_days, age_days if age_days else window_days, self.days_with_data))
        return round(total / days * days_in_month, 2)


def _ce_call(fn: Callable[..., dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    """Recorre todas las páginas (NextPageToken) de una llamada de Cost Explorer."""
    pages, token = [], None
    while True:
        resp = fn(**({**kw, "NextPageToken": token} if token else kw))
        pages.append(resp)
        token = resp.get("NextPageToken")
        if not token:
            return pages


def fetch_resource_costs(ce, *, today: date | None = None, metric: str = DEFAULT_METRIC,
                         window_days: int = RESOURCE_WINDOW_DAYS) -> dict[str, ResourceCost]:
    """Costo diario por recurso EC2/EBS/snapshot de los últimos `window_days` días (máx. 14), indexado por ID normalizado."""
    metric = metric if metric in COST_METRICS else DEFAULT_METRIC
    end = today or date.today()
    start = end - timedelta(days=min(window_days, RESOURCE_WINDOW_DAYS))
    out: dict[str, ResourceCost] = {}
    pages = _ce_call(
        ce.get_cost_and_usage_with_resources,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="DAILY", Metrics=[metric],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": list(SERVICES_EC2)}},
        GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"}])
    for resp in pages:
        for bucket in resp.get("ResultsByTime", []):
            day = date.fromisoformat(bucket["TimePeriod"]["Start"])
            for group in bucket.get("Groups", []):
                rid = normalize_resource_id(group["Keys"][0])
                amount = float(group["Metrics"][metric]["Amount"])
                if rid and rid != "NoResourceId":
                    out.setdefault(rid, ResourceCost()).daily.append((day, amount))
    return out


def fetch_tag_history(ce, tag_key: str, *, months: int = 6, today: date | None = None,
                      metric: str = DEFAULT_METRIC) -> dict[str, list[tuple[str, float]]]:
    """Costo mensual por valor de la etiqueta `tag_key` (debe estar activada como etiqueta de asignación de costos).

    Devuelve {valor_de_tag: [("2026-05", 123.4), ...]} ordenado por mes. Excluye el mes en curso (incompleto).
    """
    metric = metric if metric in COST_METRICS else DEFAULT_METRIC
    months = max(1, min(months, MAX_HISTORY_MONTHS))
    end = (today or date.today()).replace(day=1)                   # primer día del mes actual (fin exclusivo)
    y, m = end.year, end.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    start = date(y, m, 1)
    out: dict[str, list[tuple[str, float]]] = {}
    pages = _ce_call(
        ce.get_cost_and_usage,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="MONTHLY", Metrics=[metric],
        GroupBy=[{"Type": "TAG", "Key": tag_key}])
    for resp in pages:
        for bucket in resp.get("ResultsByTime", []):
            month = bucket["TimePeriod"]["Start"][:7]
            for group in bucket.get("Groups", []):
                # Las claves vienen como "Clave$valor"; el valor vacío (recursos sin etiqueta) es "Clave$".
                value = group["Keys"][0].split("$", 1)[-1]
                if value:
                    out.setdefault(value, []).append((month, round(float(group["Metrics"][metric]["Amount"]), 2)))
    for series in out.values():
        series.sort()
    return out


def trend_pct(series: list[tuple[str, float]]) -> float | None:
    """Variación porcentual entre el primer y el último mes completos (None si no hay base)."""
    if len(series) < 2 or series[0][1] <= 0:
        return None
    return round((series[-1][1] - series[0][1]) / series[0][1] * 100.0, 1)
