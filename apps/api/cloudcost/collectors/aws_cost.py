"""Costos reales de AWS (Cost Explorer, boto3 ``ce``): por recurso (ID o ARN), por etiqueta de asignación y su histórico.

Solo lectura (``ce:GetCostAndUsage*``). Tres fuentes, de mayor a menor precisión:

1. ``resource``  -> ``GetCostAndUsageWithResources`` agrupado por RESOURCE_ID. AWS lo limita a los últimos 14 días y
   exige activarlo en la cuenta de pagos (Cost Explorer -> Preferencias -> "Datos a nivel de recurso").
2. ``tag``       -> ``GetCostAndUsage`` agrupado por una etiqueta de asignación de costos activada. Si varios recursos
   comparten el valor, el costo se reparte en proporción a su costo estimado y la fuente queda marcada como ``tag``.
3. ``estimate``  -> tabla de precios (domain/pricing.py). Es la única que NO sirve para justificar un apagado.

Cada llamada a Cost Explorer cuesta USD 0.01; el colector hace pocas, paginadas, y nunca por recurso.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from ..domain.pricing import DAYS_PER_MONTH

RESOURCE_WINDOW_DAYS = 14             # límite de AWS para datos a nivel de recurso
SOURCE_RESOURCE = "cost_explorer"
SOURCE_TAG = "cost_explorer_tag"
REAL_COST_SOURCES = frozenset({SOURCE_RESOURCE, SOURCE_TAG, "cur", "import"})

# Servicios cuyo costo se atribuye a recursos que sabemos inventariar o proponer (EBS y snapshots facturan en "EC2 - Other").
CE_SERVICES = [
    "Amazon Elastic Compute Cloud - Compute",
    "EC2 - Other",
    "Amazon Relational Database Service",
    "Amazon Elastic Load Balancing",
    "Amazon Simple Storage Service",
]

DailySeries = list[tuple[date, float]]


def resource_key(raw: str) -> str:
    """Identificador comparable de un recurso: acepta ID (``vol-0abc``) o ARN completo.

    ``arn:aws:ec2:us-east-1:111122223333:volume/vol-0abc`` -> ``vol-0abc``; ``arn:aws:rds:...:db:orders`` -> ``orders``.
    """
    if not raw.startswith("arn:"):
        return raw
    tail = raw.split(":", 5)[-1]
    return tail.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


@dataclass
class CostAttribution:
    """Resultado de asignar costos reales al inventario."""
    by_resource: dict[str, DailySeries] = field(default_factory=dict)     # id -> serie diaria
    source: dict[str, str] = field(default_factory=dict)                  # id -> cost_explorer | cost_explorer_tag
    warnings: list[str] = field(default_factory=list)


class CostExplorerSource:
    def __init__(self, session, *, on_error: Callable[[str, Exception], None] | None = None, today: Callable[[], date] = date.today):
        self._session = session
        self._on_error = on_error or (lambda api, exc: None)
        self._today = today
        self._client = None

    @property
    def ce(self):
        if self._client is None:
            self._client = self._session.client("ce", region_name="us-east-1")      # Cost Explorer solo existe en us-east-1
        return self._client

    # ---------------------------------------------------------------- 1) por recurso (ID o ARN)
    def resource_costs(self, days: int = RESOURCE_WINDOW_DAYS) -> dict[str, DailySeries]:
        days = max(1, min(days, RESOURCE_WINDOW_DAYS))
        end = self._today()
        start = end - timedelta(days=days)
        out: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        token = None
        while True:
            kw: dict[str, Any] = dict(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="DAILY",
                Metrics=["UnblendedCost"], Filter={"Dimensions": {"Key": "SERVICE", "Values": CE_SERVICES}},
                GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"}])
            if token:
                kw["NextPageToken"] = token
            resp = self.ce.get_cost_and_usage_with_resources(**kw)
            for bucket in resp["ResultsByTime"]:
                day = date.fromisoformat(bucket["TimePeriod"]["Start"])
                for g in bucket["Groups"]:
                    key = resource_key(g["Keys"][0])
                    if key and key != "NoResourceId":
                        out[key][day] += float(g["Metrics"]["UnblendedCost"]["Amount"])
            token = resp.get("NextPageToken")
            if not token:
                return {k: sorted(v.items()) for k, v in out.items()}

    # ---------------------------------------------------------------- 2) por etiqueta de asignación de costos
    def tag_costs(self, tag_key: str, days: int = RESOURCE_WINDOW_DAYS) -> dict[str, DailySeries]:
        end = self._today()
        start = end - timedelta(days=max(1, days))
        out: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        token = None
        while True:
            kw: dict[str, Any] = dict(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="DAILY",
                Metrics=["UnblendedCost"], GroupBy=[{"Type": "TAG", "Key": tag_key}])
            if token:
                kw["NextPageToken"] = token
            resp = self.ce.get_cost_and_usage(**kw)
            for bucket in resp["ResultsByTime"]:
                day = date.fromisoformat(bucket["TimePeriod"]["Start"])
                for g in bucket["Groups"]:
                    value = g["Keys"][0].split("$", 1)[-1]               # AWS devuelve "clave$valor"; vacío = sin etiqueta
                    if value:
                        out[value][day] += float(g["Metrics"]["UnblendedCost"]["Amount"])
            token = resp.get("NextPageToken")
            if not token:
                return {k: sorted(v.items()) for k, v in out.items()}

    # ---------------------------------------------------------------- 3) histórico (hasta 13 meses, para reportes y tendencias)
    def monthly_history(self, months: int = 12, *, group_by: str = "SERVICE", tag_key: str | None = None) -> list[dict[str, Any]]:
        """Costo mensual de la cuenta, agrupado por servicio o por etiqueta. Devuelve ``[{month, key, amount}]``."""
        months = max(1, min(months, 13))
        today = self._today()
        first = date(today.year, today.month, 1)
        y, m = first.year, first.month - months
        while m <= 0:
            y, m = y - 1, m + 12
        start = date(y, m, 1)
        group = {"Type": "TAG", "Key": tag_key} if group_by == "TAG" and tag_key else {"Type": "DIMENSION", "Key": "SERVICE"}
        rows: list[dict[str, Any]] = []
        token = None
        while True:
            kw: dict[str, Any] = dict(TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
                                      Granularity="MONTHLY", Metrics=["UnblendedCost"], GroupBy=[group])
            if token:
                kw["NextPageToken"] = token
            resp = self.ce.get_cost_and_usage(**kw)
            for bucket in resp["ResultsByTime"]:
                month = bucket["TimePeriod"]["Start"][:7]
                for g in bucket["Groups"]:
                    rows.append({"month": month, "key": g["Keys"][0].split("$", 1)[-1] or "(sin etiqueta)",
                                 "amount": round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 2)})
            token = resp.get("NextPageToken")
            if not token:
                return rows


def monthly_from_series(series: DailySeries) -> float:
    """Costo mensual promedio a partir de una serie diaria (días sin dato cuentan como 0 solo si la serie los incluye)."""
    if not series:
        return 0.0
    return round(sum(a for _, a in series) / len(series) * DAYS_PER_MONTH, 2)


def attribute_costs(resources, source: CostExplorerSource, *, tag_key: str | None = None) -> CostAttribution:
    """Asigna costos reales a ``resources`` (NormalizedResource). Primero por ID/ARN y, para los que falten, por etiqueta."""
    attr = CostAttribution()
    wanted = {r.resource_id for r in resources}
    try:
        for key, series in source.resource_costs().items():
            if key in wanted and series:
                attr.by_resource[key] = series
                attr.source[key] = SOURCE_RESOURCE
    except Exception as exc:                                  # no es fatal: se cae a etiquetas y luego a la estimación
        source._on_error("ce:GetCostAndUsageWithResources", exc)
        attr.warnings.append(f"Cost Explorer por recurso no disponible ({type(exc).__name__}); se usa etiqueta o estimación")

    missing = [r for r in resources if r.resource_id not in attr.by_resource]
    if tag_key and missing:
        try:
            by_value = source.tag_costs(tag_key)
        except Exception as exc:
            source._on_error("ce:GetCostAndUsage", exc)
            attr.warnings.append(f"Cost Explorer por etiqueta '{tag_key}' no disponible ({type(exc).__name__})")
            by_value = {}
        groups: dict[str, list] = defaultdict(list)
        for r in missing:
            value = (r.tags or {}).get(tag_key)
            if value and value in by_value:
                groups[value].append(r)
        for value, members in groups.items():
            weights = [max(r.monthly_cost, 0.01) for r in members]
            total = sum(weights)
            for r, w in zip(members, weights, strict=True):
                share = w / total
                attr.by_resource[r.resource_id] = [(d, round(a * share, 4)) for d, a in by_value[value]]
                attr.source[r.resource_id] = SOURCE_TAG
    return attr
