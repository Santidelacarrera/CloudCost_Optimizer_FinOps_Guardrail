"""Tamaño y coste de lo que una base de datos vectorial (Qdrant) o relacional (PostgreSQL) mantiene en RAM y en disco.

Los precios por defecto son los de un nodo de propósito general (RAM ≈ USD 3,07/GiB-mes, el mismo precio por GiB que usa Kubernetes) y de disco
SSD de bloque (≈ USD 0,08/GiB-mes). Sirven para ordenar hallazgos, no para facturar: cada cuenta puede declarar los suyos (`ram_gib_month_usd`,
`disk_gib_month_usd`, `pg_storage_gib_month_usd`).
"""
from __future__ import annotations

from typing import Any

from .k8s import DEFAULT_MEM_GIB_HOUR
from .pricing import HOURS_PER_MONTH

GIB = 2**30
MIB = 2**20
DEFAULT_RAM_GIB_MONTH = round(DEFAULT_MEM_GIB_HOUR * HOURS_PER_MONTH, 4)       # 3,066 USD por GiB-mes de RAM
DEFAULT_DISK_GIB_MONTH = 0.08                                                  # SSD de bloque (gp3)
DEFAULT_PG_STORAGE_GIB_MONTH = 0.115                                           # almacenamiento de una base gestionada

# Qdrant guarda cada vector como `datatype`; el índice HNSW y los metadatos añaden alrededor de un 50 % (guía de dimensionamiento de Qdrant).
VECTOR_BYTES = {"float32": 4, "float16": 2, "uint8": 1}
QDRANT_RAM_OVERHEAD = 1.5
SCALAR_INT8_RAM_REDUCTION = 0.75           # int8 guarda 1 byte por dimensión en lugar de 4: la RAM de los vectores baja ~75 %
NONPROD_ENVIRONMENTS = ("development", "test", "staging")


def vector_specs(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Vectores densos de una colección: [{name, size, datatype, on_disk}]. Acepta un vector sin nombre o un diccionario de vectores con nombre."""
    vectors = (params or {}).get("vectors") or {}
    if isinstance(vectors, dict) and "size" in vectors:
        vectors = {"": vectors}
    out = []
    for name, spec in (vectors or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("size"), int) and spec["size"] > 0:
            out.append({"name": name, "size": spec["size"], "datatype": str(spec.get("datatype") or "float32").lower(),
                        "on_disk": bool(spec.get("on_disk")), "quantized": bool(spec.get("quantization_config"))})
    return out


def raw_vector_bytes(points: int, specs: list[dict[str, Any]], *, only_in_ram: bool = False) -> int:
    """Bytes de los vectores sin sobrecarga: puntos × Σ dimensiones × bytes por dimensión."""
    total = 0
    for s in specs:
        if only_in_ram and s["on_disk"]:
            continue
        total += points * s["size"] * VECTOR_BYTES.get(s["datatype"], 4)
    return int(total)


def qdrant_ram_bytes(points: int, specs: list[dict[str, Any]]) -> int:
    """RAM estimada de los vectores que se sirven desde memoria (los `on_disk` quedan fuera): bytes crudos × 1,5."""
    return int(raw_vector_bytes(points, specs, only_in_ram=True) * QDRANT_RAM_OVERHEAD)


def has_quantization(quantization_config: Any) -> bool:
    return bool(quantization_config)
