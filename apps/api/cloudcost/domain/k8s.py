"""Cantidades de Kubernetes (CPU en cores, memoria en bytes) y costo de lo que un workload RESERVA con sus requests.

Los precios por defecto son los de un nodo de propósito general (≈ USD 0,096/h por 2 vCPU + 8 GiB, repartido entre CPU y memoria):
sirven para ordenar hallazgos, no para facturar. Cada clúster puede declarar los suyos (`cpu_hour_usd`, `mem_gib_hour_usd`).
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

from .pricing import HOURS_PER_MONTH

DEFAULT_CPU_CORE_HOUR = 0.0316
DEFAULT_MEM_GIB_HOUR = 0.0042
MIB = 2**20
GIB = 2**30

_SUFFIX = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
           "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_QTY = re.compile(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(m|[kMGTPE]i?)?$")


class QuantityError(ValueError):
    pass


def _parse(text: str | int | float) -> Decimal:
    if isinstance(text, bool):
        raise QuantityError("cantidad inválida")
    raw = str(text).strip()
    m = _QTY.match(raw)
    if not m:
        raise QuantityError(f"cantidad inválida: {raw!r}")
    try:
        number = Decimal(m.group(1))
    except InvalidOperation as exc:
        raise QuantityError(f"cantidad inválida: {raw!r}") from exc
    unit = m.group(2) or ""
    return number / 1000 if unit == "m" else number * _SUFFIX[unit]


def parse_cpu(text: str | int | float) -> float:
    """`500m` -> 0.5 ; `2` -> 2.0 ; `0.25` -> 0.25."""
    value = float(_parse(text))
    if value < 0:
        raise QuantityError("cantidad negativa")
    return value


def parse_memory(text: str | int | float) -> int:
    """`256Mi` -> 268435456 ; `1G` -> 1000000000 ; `1.5Gi` -> 1610612736."""
    value = _parse(text)
    if value < 0:
        raise QuantityError("cantidad negativa")
    return int(value)


def format_cpu(cores: float) -> str:
    """Siempre en milicores redondeados HACIA ARRIBA (nunca se recorta de más): 0.1234 -> `124m`; 2.0 -> `2`."""
    milli = math.ceil(round(cores * 1000, 6))
    return str(milli // 1000) if milli % 1000 == 0 else f"{milli}m"


def format_memory(num_bytes: float) -> str:
    """Mi (o Gi si es múltiplo exacto), redondeado hacia arriba."""
    mib = math.ceil(round(num_bytes / MIB, 6))
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def round_up_cpu(cores: float, step: float = 0.01) -> float:
    return round(math.ceil(round(cores / step, 6)) * step, 6)


def round_up_memory(num_bytes: float, step: int = 16 * MIB) -> int:
    return int(math.ceil(round(num_bytes / step, 6)) * step)


def reserved_monthly_cost(cpu_cores: float, mem_bytes: float, replicas: int,
                          cpu_hour: float = DEFAULT_CPU_CORE_HOUR, mem_gib_hour: float = DEFAULT_MEM_GIB_HOUR) -> float:
    """Costo mensual de lo reservado (requests × réplicas)."""
    per_replica = cpu_cores * cpu_hour + (mem_bytes / GIB) * mem_gib_hour
    return round(per_replica * HOURS_PER_MONTH * max(1, replicas), 2)
