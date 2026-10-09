"""Contrato de los conectores cloud (solo lectura)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..domain.models import NormalizedResource


@dataclass
class CostRecord:
    resource_id: str
    usage_date: date
    amount: float
    service: str
    region: str | None = None
    source: str = "estimate"


@dataclass
class AccountCostRecord:
    """Coste agregado de una cuenta en un período, ya normalizado (mismo modelo para cualquier proveedor).

    `service` es el servicio normalizado (ec2, rds, s3...); `service_raw` conserva el nombre del proveedor para no perder información.
    Las cantidades se guardan en su moneda original: nunca se suman monedas distintas. `estimated` marca períodos que el proveedor
    todavía puede modificar (el mes en curso, los últimos días).
    """
    provider: str
    account_ref: str
    service: str
    service_raw: str
    region: str
    granularity: str                # DAILY | MONTHLY
    period_start: date
    period_end: date                # exclusivo
    amount: float
    currency: str = "USD"
    metric: str = "UnblendedCost"
    estimated: bool = False
    source: str = "cost_explorer"


@dataclass
class CollectionResult:
    resources: list[NormalizedResource] = field(default_factory=list)
    costs: list[CostRecord] = field(default_factory=list)
    account_costs: list[AccountCostRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)          # errores clasificados (permisos, límites, datos no disponibles...)
    data_quality: dict = field(default_factory=dict)          # cobertura: qué se pudo medir y qué no
    partial: bool = False          # True si alguna API falló: no se debe marcar como inactivo lo que no se vio


class Collector(Protocol):
    def collect(self) -> CollectionResult: ...
