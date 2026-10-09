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
    fam: str = ""                       # familia explícita (Azure/GCP no usan el punto de AWS)

    @property
    def family(self) -> str:
        return self.fam or self.name.split(".", 1)[0]

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
# Azure (Linux, pago por uso, East US) y GCP (Compute Engine, us-central1, sin descuentos): aproximaciones del mismo orden que AWS.
_AZURE: list[InstanceSpec] = [
    InstanceSpec(f"Standard_{size}", vcpu, mem, price, fam)
    for fam, rows in {
        "azure-Dsv5": [("D2s_v5", 2, 8, 0.096), ("D4s_v5", 4, 16, 0.192), ("D8s_v5", 8, 32, 0.384), ("D16s_v5", 16, 64, 0.768)],
        "azure-Esv5": [("E2s_v5", 2, 16, 0.126), ("E4s_v5", 4, 32, 0.252), ("E8s_v5", 8, 64, 0.504), ("E16s_v5", 16, 128, 1.008)],
        "azure-Fsv2": [("F2s_v2", 2, 4, 0.085), ("F4s_v2", 4, 8, 0.169), ("F8s_v2", 8, 16, 0.338), ("F16s_v2", 16, 32, 0.677)],
        "azure-Bms": [("B2s", 2, 4, 0.0416), ("B2ms", 2, 8, 0.0832), ("B4ms", 4, 16, 0.166), ("B8ms", 8, 32, 0.333)],
    }.items() for size, vcpu, mem, price in rows
]
_GCP: list[InstanceSpec] = [
    InstanceSpec(f"{fam}-{n}", n, n * ratio, round(n * per_vcpu, 4), f"gcp-{fam}")
    for fam, ratio, per_vcpu in (("n2-standard", 4, 0.0485), ("n2-highmem", 8, 0.0655), ("e2-standard", 4, 0.0335))
    for n in (2, 4, 8, 16)
]
_SPECS.extend(_AZURE + _GCP)
INSTANCE_SPECS: dict[str, InstanceSpec] = {s.name: s for s in _SPECS}

# USD por GB-mes
VOLUME_GB_MONTH = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05,
                   # Azure Managed Disks (por SKU; el cobro real es por escalón de tamaño: aproximación lineal)
                   "standard_lrs": 0.045, "standardssd_lrs": 0.075, "premium_lrs": 0.15, "standard_zrs": 0.056,
                   "standardssd_zrs": 0.094, "premium_zrs": 0.19,
                   # GCP Persistent Disk
                   "pd-standard": 0.04, "pd-balanced": 0.10, "pd-ssd": 0.17}
SNAPSHOT_GB_MONTH = 0.05
SNAPSHOT_GB_MONTH_BY_PROVIDER = {"aws": 0.05, "azure": 0.05, "gcp": 0.026}


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


def snapshot_monthly_cost(size_gb: float | None, provider: str = "aws") -> float:
    """Cota superior: los snapshots son incrementales, el costo real suele ser menor."""
    return round(SNAPSHOT_GB_MONTH_BY_PROVIDER.get(provider, SNAPSHOT_GB_MONTH) * float(size_gb or 0), 2)


# RDS (MySQL/PostgreSQL, us-east-1, On-Demand, Single-AZ): USD por hora de instancia y USD por GB-mes de almacenamiento gp2/gp3.
RDS_HOURLY = {"db.t3.medium": 0.072, "db.t3.large": 0.145, "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.r5.large": 0.240,
              "db.r5.xlarge": 0.480}
RDS_STORAGE_GB_MONTH = 0.115


def rds_monthly_cost(db_class: str | None, storage_gb: float | None, multi_az: bool = False) -> float:
    """Instancia + almacenamiento; Multi-AZ duplica ambos. Clase desconocida: solo almacenamiento (el costo real manda cuando existe)."""
    hourly = RDS_HOURLY.get(db_class or "", 0.0)
    total = hourly * HOURS_PER_MONTH + RDS_STORAGE_GB_MONTH * float(storage_gb or 0)
    return round(total * (2 if multi_az else 1), 2)
