"""Costos REALES de AWS vía Cost Explorer (solo lectura).

Tres fuentes complementarias, porque AWS no da más con una sola llamada:

* `GetCostAndUsageWithResources` — costo DIARIO por recurso. AWS lo limita a los últimos 14 días. Es el dato que alimenta
  `current_monthly_cost` (el costo con el que se calcula el ahorro) antes de proponer apagar o reducir algo.
* `GetCostAndUsage` agrupado por SERVICIO y REGIÓN, filtrado por cuenta — costo de la cuenta por servicio/región/período:
  DIARIO para el último mes y MENSUAL para el historial (hasta 12 meses). Es el contexto con el que se controlan los cambios
  de uso al medir el ahorro y lo que alimenta los informes por cuenta (`account_costs`).
* `GetCostAndUsage` agrupado por TAG — costo MENSUAL por valor de etiqueta, hasta 12 meses. El costo de una etiqueta agrupa a
  todos los recursos que la comparten: se informa como tal (`scope: "tag"`), nunca se reparte a un recurso individual.

Todas las llamadas son lecturas. Un fallo de Cost Explorer NO invalida el inventario: se degrada a la tabla de precios y se avisa.
Cada solicitud a Cost Explorer cuesta USD 0,01 en la cuenta de pagos: `CostExplorerBudget` limita cuántas hace un escaneo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import fmean, median
from typing import Any, Callable

from ..domain.cost_quality import BLOCKING_FLAGS, CONFIDENCE_FLAGS  # noqa: F401  (se reexportan para los llamadores)
from .base import AccountCostRecord

RESOURCE_WINDOW_DAYS = 14               # límite de AWS para costos por recurso
MAX_HISTORY_MONTHS = 12                 # límite de AWS para granularidad mensual
ACCOUNT_DAILY_DAYS = 35                 # ventana diaria por cuenta: cubre 14 días previos + 14 posteriores a un cambio
MAX_PAGES = 100                         # corta paginaciones que no terminan (token que se repite)
SERVICES_EC2 = ("Amazon Elastic Compute Cloud - Compute", "EC2 - Other")   # EC2 - Other incluye EBS y snapshots
COST_METRICS = ("UnblendedCost", "AmortizedCost", "NetUnblendedCost", "NetAmortizedCost")
DEFAULT_METRIC = "UnblendedCost"
DEFAULT_CE_BUDGET = 60                  # solicitudes por escaneo (≈ USD 0,60 como máximo)

# Calidad de la serie diaria de un recurso (ver ResourceCost.quality_flags)
SPIKE_FACTOR = 3.0                      # un día > 3× la mediana es un pico aislado
TREND_BREAK_PCT = 0.30                  # los últimos días difieren > 30 % del resto: cambio de nivel (cambio de tamaño, parada...)
RECENT_DAYS = 3

# Nombre del servicio en Cost Explorer -> servicio normalizado. Lo no reconocido se conserva tal cual en `service_raw` y pasa a «other».
SERVICE_MAP = {
    "Amazon Elastic Compute Cloud - Compute": "ec2",
    "EC2 - Other": "ec2_other",                                  # EBS, snapshots, NAT, transferencia...
    "Amazon Relational Database Service": "rds",
    "Amazon Simple Storage Service": "s3",
    "Amazon Elastic Block Store": "ebs",
    "Amazon Elastic Load Balancing": "elb",
    "Amazon Virtual Private Cloud": "vpc",
    "Amazon CloudWatch": "cloudwatch",
    "AmazonCloudWatch": "cloudwatch",
    "Amazon Elastic Container Service for Kubernetes": "eks",
    "Amazon Elastic Kubernetes Service": "eks",
    "Amazon Elastic Container Service": "ecs",
    "AWS Lambda": "lambda",
    "Amazon DynamoDB": "dynamodb",
    "Amazon ElastiCache": "elasticache",
    "Amazon Route 53": "route53",
    "Amazon CloudFront": "cloudfront",
    "AWS Key Management Service": "kms",
    "AWS Secrets Manager": "secretsmanager",
    "AWS Backup": "backup",
    "Tax": "tax",
}


def normalize_service(raw: str) -> str:
    return SERVICE_MAP.get((raw or "").strip(), "other")


def normalize_region(raw: str | None) -> str:
    value = (raw or "").strip()
    return "global" if value.lower() in ("", "global", "noregion", "no region") else value


def normalize_resource_id(raw: str) -> str:
    """`arn:aws:ec2:us-east-1:123:instance/i-0abc` -> `i-0abc`. Los IDs que ya son simples se devuelven igual."""
    value = (raw or "").strip()
    if value.startswith("arn:"):
        tail = value.split(":", 5)[-1]            # recurso: `instance/i-0abc` o `volume/vol-1`
        return tail.rsplit("/", 1)[-1]
    return value


class CostExplorerBudgetExceeded(Exception):
    """Se agotó el presupuesto de solicitudes a Cost Explorer de este escaneo (cada solicitud cuesta dinero)."""


@dataclass
class CostExplorerBudget:
    limit: int = DEFAULT_CE_BUDGET
    used: int = 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise CostExplorerBudgetExceeded(f"presupuesto de {self.limit} solicitudes a Cost Explorer agotado")
        self.used += 1


def series_flags(values: list[float], *, expected_days: int | None = None) -> list[str]:
    """Señales de que el promedio de una serie diaria NO representa el coste actual del recurso.

    negative_amount   créditos o reembolsos: la línea base no es fiable (bloquea el uso del coste como verificado)
    spike             un día aislado > 3× la mediana: el promedio se infla
    trend_break       los últimos días difieren > 30 % del resto: el recurso cambió (tamaño, parada, reinicio)
    partial_window    hay menos días con datos de los esperados
    """
    flags: list[str] = []
    if not values:
        return flags
    if any(v < 0 for v in values):
        flags.append("negative_amount")
    med = median(values)
    if len(values) >= 5 and med > 0 and max(values) > SPIKE_FACTOR * med:
        flags.append("spike")
    if len(values) >= 7:
        earlier, recent = values[:-RECENT_DAYS], values[-RECENT_DAYS:]
        base = fmean(earlier)
        if base > 0 and abs(fmean(recent) - base) / base > TREND_BREAK_PCT:
            flags.append("trend_break")
    if expected_days is not None and len(values) < expected_days:
        flags.append("partial_window")
    return flags


def series_rate(values: list[float], flags: list[str]) -> float:
    """Coste diario representativo. Prudente ante señales de calidad (nunca infla el ahorro):

    · sin señales: media de los días con dato;
    · pico aislado: mediana (no se promedia el pico);
    · cambio de nivel: el menor entre la media de la ventana y la de los últimos días.
    """
    if not values:
        return 0.0
    rate = fmean(values)
    if "spike" in flags:
        rate = min(rate, median(values))
    if "trend_break" in flags and len(values) >= RECENT_DAYS:
        rate = min(rate, fmean(values[-RECENT_DAYS:]))
    return max(0.0, rate)


@dataclass
class ResourceCost:
    daily: list[tuple[date, float]] = field(default_factory=list)

    @property
    def days_with_data(self) -> int:
        return len({d for d, _ in self.daily})

    def values(self, last: int | None = None) -> list[float]:
        merged: dict[date, float] = {}
        for d, a in self.daily:                      # un mismo día puede venir en varias filas (varios tipos de uso)
            merged[d] = merged.get(d, 0.0) + a
        out = [merged[d] for d in sorted(merged)]
        return out[-last:] if last else out

    def quality_flags(self, *, expected_days: int | None = None, last: int | None = None) -> list[str]:
        return series_flags(self.values(last), expected_days=expected_days)

    def monthly_estimate(self, days_in_month: float, *, window_days: int = RESOURCE_WINDOW_DAYS,
                         age_days: int | None = None) -> float:
        """Costo mensual = coste diario representativo × días del mes (ver `series_rate`).

        Si el recurso es más joven que la ventana, solo cuentan los días de su vida (no se divide por días sin existir).
        """
        values = self.values()
        if not values:
            return 0.0
        days = max(1, min(window_days, age_days if age_days else window_days, len(values)))
        values = values[-days:]
        return round(series_rate(values, series_flags(values)) * days_in_month, 2)


def _ce_call(fn: Callable[..., dict[str, Any]], *, budget: CostExplorerBudget | None = None,
             retry: Callable[[Callable[[], dict[str, Any]]], dict[str, Any]] | None = None, **kw: Any) -> list[dict[str, Any]]:
    """Recorre todas las páginas (NextPageToken) de una llamada de Cost Explorer.

    Todo o nada: si una página falla se relanza la excepción (con la lista parcial en `exc.partial_pages`) para que el llamador NO use
    datos con días o recursos ausentes como si estuvieran completos.
    """
    pages: list[dict[str, Any]] = []
    token, seen = None, set()
    while True:
        if len(pages) >= MAX_PAGES:
            raise RuntimeError("paginación de Cost Explorer sin fin")
        params = {**kw, "NextPageToken": token} if token else kw

        def attempt(params: dict[str, Any] = params) -> dict[str, Any]:
            if budget:
                budget.spend()                              # cada intento cuenta: AWS cobra por solicitud
            return fn(**params)

        try:
            resp = retry(attempt) if retry else attempt()
        except Exception as exc:
            exc.partial_pages = pages                      # type: ignore[attr-defined]
            raise
        pages.append(resp)
        token = resp.get("NextPageToken")
        if not token:
            return pages
        if token in seen:
            raise RuntimeError("Cost Explorer repitió un NextPageToken")
        seen.add(token)


def fetch_resource_costs(ce, *, today: date | None = None, metric: str = DEFAULT_METRIC,
                         window_days: int = RESOURCE_WINDOW_DAYS, budget: CostExplorerBudget | None = None,
                         retry=None) -> dict[str, ResourceCost]:
    """Costo diario por recurso EC2/EBS/snapshot de los últimos `window_days` días (máx. 14), indexado por ID normalizado."""
    metric = metric if metric in COST_METRICS else DEFAULT_METRIC
    end = today or date.today()
    start = end - timedelta(days=min(window_days, RESOURCE_WINDOW_DAYS))
    out: dict[str, ResourceCost] = {}
    pages = _ce_call(
        ce.get_cost_and_usage_with_resources, budget=budget, retry=retry,
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
                      metric: str = DEFAULT_METRIC, budget: CostExplorerBudget | None = None,
                      retry=None) -> dict[str, list[tuple[str, float]]]:
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
        ce.get_cost_and_usage, budget=budget, retry=retry,
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


# --------------------------------------------------------------------------- costes por cuenta · servicio · región · período
def month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def fetch_account_costs(ce, account_ref: str | None, *, granularity: str, start: date, end: date,
                        metric: str = DEFAULT_METRIC, budget: CostExplorerBudget | None = None, retry=None,
                        provider: str = "aws") -> list[AccountCostRecord]:
    """Costo de UNA cuenta por servicio y región para [start, end) con granularidad DAILY o MONTHLY, normalizado al modelo interno.

    `account_ref` debe ser el ID de 12 dígitos; con otro valor se omite el filtro (el costo podría incluir cuentas vinculadas).
    Cost Explorer admite como máximo 2 agrupaciones: SERVICE × REGION; la cuenta se fija con un filtro LINKED_ACCOUNT.
    Se conserva la moneda (`Unit`) de cada fila y la marca `Estimated` del período.
    """
    if granularity not in ("DAILY", "MONTHLY"):
        raise ValueError("granularity debe ser DAILY o MONTHLY")
    if end <= start:
        return []
    metric = metric if metric in COST_METRICS else DEFAULT_METRIC
    kw: dict[str, Any] = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()}, "Granularity": granularity, "Metrics": [metric],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "REGION"}]}
    if account_ref and account_ref.isdigit() and len(account_ref) == 12:
        kw["Filter"] = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_ref]}}
    pages = _ce_call(ce.get_cost_and_usage, budget=budget, retry=retry, **kw)
    merged: dict[tuple, AccountCostRecord] = {}
    for resp in pages:
        for bucket in resp.get("ResultsByTime", []):
            p_start = date.fromisoformat(bucket["TimePeriod"]["Start"])
            p_end = date.fromisoformat(bucket["TimePeriod"]["End"])
            estimated = bool(bucket.get("Estimated", False))
            for group in bucket.get("Groups", []):
                service_raw = group["Keys"][0]
                region = normalize_region(group["Keys"][1] if len(group["Keys"]) > 1 else None)
                m = group["Metrics"][metric]
                amount, currency = float(m["Amount"]), m.get("Unit") or "USD"
                key = (service_raw, region, p_start, currency)
                if key in merged:                                  # la misma clave en varias páginas se suma, no se pisa
                    merged[key].amount += amount
                    continue
                merged[key] = AccountCostRecord(
                    provider=provider, account_ref=account_ref or "", service=normalize_service(service_raw),
                    service_raw=service_raw, region=region, granularity=granularity, period_start=p_start, period_end=p_end,
                    amount=amount, currency=currency, metric=metric, estimated=estimated)
    return sorted(merged.values(), key=lambda r: (r.period_start, r.service_raw, r.region))
