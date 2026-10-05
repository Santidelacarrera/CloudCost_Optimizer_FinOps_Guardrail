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
class CollectionResult:
    resources: list[NormalizedResource] = field(default_factory=list)
    costs: list[CostRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False          # True si alguna API falló: no se debe marcar como inactivo lo que no se vio


class Collector(Protocol):
    def collect(self) -> CollectionResult: ...
