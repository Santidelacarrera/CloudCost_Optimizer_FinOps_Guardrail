"""Modelo normalizado de recursos (común a AWS, Azure y GCP) y utilidades de etiquetas."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------- entornos
_ENV_ALIASES = {
    "prod": "production", "production": "production", "prd": "production", "live": "production",
    "stg": "staging", "stage": "staging", "staging": "staging", "preprod": "staging", "uat": "staging",
    "dev": "development", "development": "development", "sandbox": "development",
    "test": "test", "testing": "test", "qa": "test",
}
_ENV_TAG_KEYS = ("environment", "env", "stage")

# Etiquetas que protegen un recurso: ninguna regla propondrá acciones sobre él.
_PROTECTION_KEYS = {"finops:ignore", "finops-ignore", "do-not-delete", "donotdelete", "retain", "legal-hold", "keep"}
_FALSE_VALUES = {"false", "0", "no", "off", ""}


def environment_from_tags(tags: dict[str, str] | None) -> str:
    """Devuelve production|staging|development|test|unknown. Lo desconocido se trata como producción en las políticas."""
    for key, value in (tags or {}).items():
        if key.strip().lower() in _ENV_TAG_KEYS:
            return _ENV_ALIASES.get(str(value).strip().lower(), "unknown")
    return "unknown"


def is_protected(tags: dict[str, str] | None) -> bool:
    for key, value in (tags or {}).items():
        if key.strip().lower() in _PROTECTION_KEYS and str(value).strip().lower() not in _FALSE_VALUES:
            return True
    return False


# Vocabulario común: cada nube nombra distinto lo mismo; las reglas trabajan con estas tres familias.
COMPUTE_SERVICES = frozenset({"ec2", "vm", "gce"})                     # AWS EC2 | Azure VM | GCP Compute Engine
VOLUME_SERVICES = frozenset({"ebs", "disk", "pd"})                     # EBS | Azure Managed Disk | Persistent Disk
SNAPSHOT_SERVICES = frozenset({"ebs_snapshot", "disk_snapshot", "pd_snapshot"})
PROVIDERS = ("aws", "azure", "gcp")

# (proveedor, servicio) -> tipos de recurso de Terraform que lo declaran (el primero es el que se muestra).
TF_TYPES: dict[tuple[str, str], tuple[str, ...]] = {
    ("aws", "ec2"): ("aws_instance",), ("aws", "ebs"): ("aws_ebs_volume",), ("aws", "ebs_snapshot"): ("aws_ebs_snapshot",),
    ("aws", "rds"): ("aws_db_instance",),
    ("azure", "vm"): ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"),
    ("azure", "disk"): ("azurerm_managed_disk",), ("azure", "disk_snapshot"): ("azurerm_snapshot",),
    ("gcp", "gce"): ("google_compute_instance",), ("gcp", "pd"): ("google_compute_disk", "google_compute_region_disk"),
    ("gcp", "pd_snapshot"): ("google_compute_snapshot",),
}
# Atributo de Terraform que fija el tamaño de la máquina, por tipo.
RESIZE_ATTR = {"aws_instance": "instance_type", "azurerm_linux_virtual_machine": "size",
               "azurerm_windows_virtual_machine": "size", "google_compute_instance": "machine_type"}


def tf_types_for(res: "NormalizedResource") -> tuple[str, ...]:
    return TF_TYPES.get((res.provider, res.service), ())


def tf_type_for(res: "NormalizedResource") -> str | None:
    types = tf_types_for(res)
    return types[0] if types else None


@dataclass
class NormalizedResource:
    provider: str                       # aws | azure | gcp
    resource_type: str                  # compute | storage | database | kubernetes
    service: str                        # ec2 | ebs | ebs_snapshot | rds | vm | disk | disk_snapshot | gce | pd | pd_snapshot
    resource_id: str
    region: str
    name: str | None = None
    environment: str = "unknown"
    instance_type: str | None = None
    volume_type: str | None = None
    state: str | None = None
    monthly_cost: float = 0.0
    cost_source: str = "estimate"
    cpu_avg: float | None = None
    cpu_max: float | None = None
    memory_avg: float | None = None
    attached: bool | None = None
    size_gb: float | None = None
    age_days: int | None = None
    observation_days: int = 0
    unattached_days: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    iac_address: str | None = None
    iac_file: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
