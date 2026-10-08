"""Tabla de precios APROXIMADOS (AWS us-east-1, Linux on-demand, USD) para estimar costos y ahorro.

Es una aproximación para el MVP. En producción, reemplazar por la AWS Price List API o por costos
reales (Cost Explorer / CUR); el campo `cost_source` de los recursos indica el origen del dato.
"""
from __future__ import annotations

from dataclasses import dataclass

HOURS_PER_MONTH = 730.0
DAYS_PER_MONTH = 30.4375


@dataclass(frozen=True)
class InstanceSpec:
    name: str
    vcpu: int
    memory_gib: float
    hourly_usd: float

    @property
    def family(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def monthly_usd(self) -> float:
        return round(self.hourly_usd * HOURS_PER_MONTH, 2)


def _fam(family: str, rows: list[tuple[str, int, float, float]]) -> list[InstanceSpec]:
    return [InstanceSpec(f"{family}.{size}", vcpu, mem, price) for size, vcpu, mem, price in rows]


_SPECS: list[InstanceSpec] = (
    _fam("m5", [("large", 2, 8, 0.096), ("xlarge", 4, 16, 0.192), ("2xlarge", 8, 32, 0.384), ("4xlarge", 16, 64, 0.768)])
    + _fam("c5", [("large", 2, 4, 0.085), ("xlarge", 4, 8, 0.170), ("2xlarge", 8, 16, 0.340), ("4xlarge", 16, 32, 0.680)])
    + _fam("r5", [("large", 2, 16, 0.126), ("xlarge", 4, 32, 0.252), ("2xlarge", 8, 64, 0.504), ("4xlarge", 16, 128, 1.008)])
    + _fam("t3", [("nano", 2, 0.5, 0.0052), ("micro", 2, 1, 0.0104), ("small", 2, 2, 0.0208), ("medium", 2, 4, 0.0416),
                  ("large", 2, 8, 0.0832), ("xlarge", 4, 16, 0.1664), ("2xlarge", 8, 32, 0.3328)])
)
INSTANCE_SPECS: dict[str, InstanceSpec] = {s.name: s for s in _SPECS}

# USD por GB-mes
VOLUME_GB_MONTH = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
SNAPSHOT_GB_MONTH = 0.05


def instance_spec(instance_type: str | None) -> InstanceSpec | None:
    return INSTANCE_SPECS.get(instance_type or "")


def instance_monthly_cost(instance_type: str | None) -> float | None:
    spec = instance_spec(instance_type)
    return spec.monthly_usd if spec else None


def smaller_types(instance_type: str, max_steps: int = 2) -> list[InstanceSpec]:
    """Tipos más baratos de la misma familia, del más cercano al más pequeño, hasta `max_steps`."""
    spec = instance_spec(instance_type)
    if not spec:
        return []
    cheaper = [s for s in _SPECS if s.family == spec.family and s.hourly_usd < spec.hourly_usd]
    cheaper.sort(key=lambda s: s.hourly_usd, reverse=True)
    return cheaper[: max(0, max_steps)]


def volume_monthly_cost(volume_type: str | None, size_gb: float | None) -> float:
    if not size_gb:
        return 0.0
    return round(VOLUME_GB_MONTH.get((volume_type or "gp2").lower(), 0.10) * float(size_gb), 2)


def snapshot_monthly_cost(size_gb: float | None) -> float:
    """Cota superior: los snapshots son incrementales, el costo real suele ser menor."""
    return round(SNAPSHOT_GB_MONTH * float(size_gb or 0), 2)


# --- RDS (PostgreSQL/MySQL, una AZ, on-demand us-east-1; Aurora y Multi-AZ se aproximan con el mismo precio por clase)
RDS_HOURLY = {"db.t3.medium": 0.072, "db.t3.large": 0.145, "db.m5.large": 0.171, "db.m5.xlarge": 0.342,
              "db.r5.large": 0.250, "db.r5.xlarge": 0.500, "db.r5.2xlarge": 1.000, "db.r5.4xlarge": 2.000}
RDS_STORAGE_GB_MONTH = 0.115


def rds_monthly_cost(instance_class: str | None, storage_gb: float | None = 0, multi_az: bool = False) -> float:
    hourly = RDS_HOURLY.get(instance_class or "", 0.0) * (2 if multi_az else 1)
    return round(hourly * HOURS_PER_MONTH + RDS_STORAGE_GB_MONTH * float(storage_gb or 0) * (2 if multi_az else 1), 2)
