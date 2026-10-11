"""Reglas de optimización de bases de datos: Qdrant (RAM de vectores, réplicas) y PostgreSQL (índices sin uso, espacio muerto).

SIEMPRE consultivas: producen un `Finding` con evidencia, fórmula y ahorro estimado, pero la acción no está en la lista de las que generan un
Pull Request (`ADVISORY_ACTIONS`): se aplica con la API de Qdrant o con SQL, y la plataforma nunca ejecuta nada. Se cargan desde `rules.py`.
"""
from __future__ import annotations

from typing import Any

from .datastores import GIB, MIB, NONPROD_ENVIRONMENTS, SCALAR_INT8_RAM_REDUCTION, raw_vector_bytes
from .models import NormalizedResource, is_protected
from .rules import (
    ACTION_DROP_UNUSED_INDEX,
    ACTION_QUANTIZE_COLLECTION,
    ACTION_RECLAIM_TABLE_BLOAT,
    ACTION_REDUCE_REPLICATION,
    Finding,
    RuleConfig,
    _clamp,
)

SCALAR_INT8 = {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}}


def _price(res: NormalizedResource, key: str, default: float) -> float:
    return float(res.attributes.get(key) or default)


def rule_qdrant_quantization(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Colección cuyos vectores viven en RAM sin cuantizar: la cuantización escalar int8 baja esa RAM ~75 % guardando los originales en disco."""
    if res.service != "qdrant_collection" or is_protected(res.tags):
        return None
    a = res.attributes
    if a.get("quantization") or not a.get("vectors"):
        return None
    ram_gib = float(a.get("ram_bytes") or 0) / GIB
    points = int(a.get("points") or 0)
    indexed = a.get("indexed_ratio")
    if ram_gib < cfg.qdrant_min_ram_gib or points <= 0 or (indexed is not None and indexed < cfg.qdrant_min_indexed_ratio):
        return None
    ram_price, disk_price = _price(res, "ram_gib_month_usd", cfg.ram_gib_month_usd), _price(res, "disk_gib_month_usd", cfg.disk_gib_month_usd)
    configured = max(1, int(a.get("replication_factor") or 1))
    # Si en un entorno que no es producción también se propone reducir las réplicas, esta cifra se calcula sobre UNA réplica: así las dos
    # recomendaciones se suman sin contar dos veces la misma RAM.
    replicas = 1 if (res.environment in NONPROD_ENVIRONMENTS and configured >= 2) else configured
    in_ram = [v for v in a["vectors"] if not v["on_disk"]]
    raw_gib = raw_vector_bytes(points, in_ram) / GIB
    saved_ram_cost = SCALAR_INT8_RAM_REDUCTION * ram_gib * ram_price * replicas
    disk_cost = raw_gib * disk_price * replicas                                  # los vectores originales pasan a disco (en cada réplica)
    savings = round(saved_ram_cost - disk_cost, 2)
    cost = float(res.monthly_cost) or ram_gib * ram_price * configured
    if savings < cfg.min_monthly_savings:
        return None
    confidence = 0.70 + (0.06 if points >= 1_000_000 else 0.0) + (0.04 if a.get("status") == "green" else -0.10)
    confidence -= 0.05 if min(v["size"] for v in in_ram) < 256 else 0.0          # con pocas dimensiones la pérdida de precisión pesa más
    return Finding(
        rule_id="qdrant_quantization", action=ACTION_QUANTIZE_COLLECTION, resource=res, destructive=False,
        title=f"Cuantizar la colección Qdrant {a.get('collection')}",
        summary=(f"{points:,} puntos con {', '.join(str(v['size']) for v in in_ram)} dimensiones ocupan ≈ {ram_gib:.2f} GiB de RAM sin cuantizar"
                 f"{f' (×{replicas} réplicas)' if replicas > 1 else ''}"
                 f"{' (calculado sobre 1 réplica: se suma al ahorro de reducir las réplicas)' if configured != replicas else ''}. La cuantización escalar int8 reduce esa RAM ≈ 75 % con los originales en disco."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=round(max(0.0, cost - savings), 2), estimated_monthly_savings=savings,
        confidence=_clamp(confidence),
        params={"collection": a.get("collection"), "quantization_config": SCALAR_INT8, "vectors_on_disk": True},
        stable_key={"collection": a.get("collection")},
        evidence={"environment": res.environment, "points": points, "vectors": [{"name": v["name"], "size": v["size"], "datatype": v["datatype"]} for v in in_ram],
                  "ram_gib": round(ram_gib, 3), "raw_vectors_gib": round(raw_gib, 3), "replication_factor": configured, "replicas_in_estimate": replicas,
                  "indexed_ratio": indexed, "ram_reduction": SCALAR_INT8_RAM_REDUCTION,
                  "price_basis": {"ram_gib_month_usd": ram_price, "disk_gib_month_usd": disk_price},
                  "savings_basis": "RAM de los vectores × 75 % × precio de RAM, menos el disco que ocupan los originales"},
        alternatives=[
            {"action": "BINARY_QUANTIZATION", "description": "Con ≥ 1024 dimensiones, la cuantización binaria reduce la RAM mucho más (≈ 32×) a cambio de más pérdida de precisión: úsala con reordenación (rescore)."},
            {"action": "VECTORS_ON_DISK", "description": "Guardar los vectores en disco (mmap) si la latencia lo permite y el disco es rápido: la RAM de vectores baja a casi cero."},
            {"action": "FLOAT16_DATATYPE", "description": "Almacenar los vectores como float16: reduce a la mitad el tamaño con una pérdida de precisión pequeña."}],
    )


def rule_qdrant_replication_nonprod(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Colección con varias réplicas en un entorno que no es producción: cada réplica adicional multiplica la RAM sin aportar disponibilidad necesaria."""
    if res.service != "qdrant_collection" or is_protected(res.tags) or res.environment not in NONPROD_ENVIRONMENTS:
        return None
    a = res.attributes
    replicas = int(a.get("replication_factor") or 1)
    ram_gib = float(a.get("ram_bytes") or 0) / GIB
    if replicas < 2 or ram_gib <= 0:
        return None
    ram_price = _price(res, "ram_gib_month_usd", cfg.ram_gib_month_usd)
    cost = float(res.monthly_cost) or ram_gib * ram_price * replicas
    savings = round((replicas - 1) * ram_gib * ram_price, 2)
    if savings < cfg.min_monthly_savings:
        return None
    return Finding(
        rule_id="qdrant_replication_nonprod", action=ACTION_REDUCE_REPLICATION, resource=res, destructive=False,
        title=f"Reducir las réplicas de {a.get('collection')} a 1 ({res.environment})",
        summary=f"Tiene replication_factor={replicas} en un entorno {res.environment}: cada réplica extra mantiene ≈ {ram_gib:.2f} GiB de RAM de vectores.",
        current_monthly_cost=round(cost, 2), projected_monthly_cost=round(max(0.0, cost - savings), 2), estimated_monthly_savings=savings,
        confidence=_clamp(0.80 + (0.04 if a.get("status") == "green" else 0.0)),
        params={"collection": a.get("collection"), "replication_factor": 1, "current": replicas},
        stable_key={"collection": a.get("collection")},
        evidence={"environment": res.environment, "replication_factor": replicas, "ram_gib_per_replica": round(ram_gib, 3),
                  "price_basis": {"ram_gib_month_usd": ram_price}, "savings_basis": "(réplicas − 1) × RAM de vectores × precio de RAM"},
        alternatives=[{"action": "KEEP_SNAPSHOTS", "description": "Con 1 réplica, programa snapshots de la colección: son tu recuperación si el nodo falla."}],
    )


def rule_pg_unused_index(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Índice sin ningún escaneo en toda la ventana de estadísticas, que no respalda una restricción: ocupa disco y encarece cada escritura."""
    if res.service != "pg_index" or is_protected(res.tags):
        return None
    a = res.attributes
    if a.get("idx_scan") != 0 or a.get("backs_constraint") or res.observation_days < cfg.pg_min_stats_days:
        return None
    size_gb = float(res.size_gb or 0)
    if size_gb * 1024 < cfg.pg_min_index_mib:
        return None
    price = _price(res, "pg_storage_gib_month_usd", cfg.pg_storage_gib_month_usd)
    cost = float(res.monthly_cost) or size_gb * price
    savings = round(cost, 2)
    if savings < min(cfg.min_monthly_savings, 1.0):                           # un índice grande ahorra poco dinero: el umbral global (USD 5) lo descartaría
        return None
    confidence = 0.72 + (0.08 if res.observation_days >= 30 else 0.0) - (0.10 if a.get("has_replicas") else 0.0)
    return Finding(
        rule_id="pg_unused_index", action=ACTION_DROP_UNUSED_INDEX, resource=res, destructive=True,
        title=f"Eliminar el índice sin uso {a.get('schema')}.{a.get('index')}",
        summary=(f"El índice {a.get('index')} de {a.get('schema')}.{a.get('table')} ocupa {size_gb * 1024:.0f} MiB y no se ha usado en {res.observation_days} días "
                 "de estadísticas. Si no respalda una restricción, eliminarlo libera disco y acelera las escrituras."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0, estimated_monthly_savings=savings, confidence=_clamp(confidence),
        params={"schema": a.get("schema"), "index": a.get("index"), "table": a.get("table"), "sql": f'DROP INDEX CONCURRENTLY "{a.get("schema")}"."{a.get("index")}";'},
        stable_key={"schema": a.get("schema"), "index": a.get("index")},
        evidence={"environment": res.environment, "idx_scan": 0, "stats_days": res.observation_days, "size_mib": round(size_gb * 1024, 1),
                  "table": a.get("table"), "price_basis": {"pg_storage_gib_month_usd": price},
                  "savings_basis": "tamaño del índice × precio de almacenamiento (el ahorro real exige reducir el volumen)"},
        alternatives=[
            {"action": "CHECK_REPLICAS", "description": "Las estadísticas son por servidor: comprueba que el índice tampoco se usa en las réplicas de lectura antes de eliminarlo."},
            {"action": "RECREATE_SCRIPT", "description": "Guarda su definición (pg_get_indexdef) para poder recrearlo si un informe poco frecuente lo necesitara."}],
    )


def rule_pg_table_bloat(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Tabla con una proporción alta de tuplas muertas: el espacio recuperable ocupa disco que se paga."""
    if res.service != "pg_table" or is_protected(res.tags):
        return None
    a = res.attributes
    live, dead = int(a.get("n_live_tup") or 0), int(a.get("n_dead_tup") or 0)
    heap = int(a.get("heap_bytes") or 0)
    ratio = dead / (live + dead) if (live + dead) else 0.0
    if ratio < cfg.pg_bloat_dead_ratio or heap < cfg.pg_bloat_min_mib * MIB:
        return None
    price = _price(res, "pg_storage_gib_month_usd", cfg.pg_storage_gib_month_usd)
    cost = float(res.monthly_cost) or (int(a.get("total_bytes") or heap) / GIB) * price
    reclaimable_gib = heap * ratio / GIB
    savings = round(min(cost, reclaimable_gib * price), 2)
    if savings < min(cfg.min_monthly_savings, 1.0):
        return None
    return Finding(
        rule_id="pg_table_bloat", action=ACTION_RECLAIM_TABLE_BLOAT, resource=res, destructive=False,
        title=f"Recuperar el espacio muerto de {a.get('schema')}.{a.get('table')}",
        summary=(f"{ratio:.0%} de las tuplas están muertas ({dead:,} de {live + dead:,}) en una tabla de {heap / MIB:.0f} MiB. "
                 "Un VACUUM (o pg_repack, sin bloquear) devuelve ese espacio al motor."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=round(max(0.0, cost - savings), 2), estimated_monthly_savings=savings, confidence=_clamp(0.60),
        params={"schema": a.get("schema"), "table": a.get("table"), "sql": f'VACUUM (ANALYZE) "{a.get("schema")}"."{a.get("table")}";'},
        stable_key={"schema": a.get("schema"), "table": a.get("table")},
        evidence={"environment": res.environment, "dead_ratio": round(ratio, 3), "n_live_tup": live, "n_dead_tup": dead, "heap_mib": round(heap / MIB, 1),
                  "reclaimable_gib": round(reclaimable_gib, 3), "last_autovacuum": a.get("last_autovacuum"),
                  "price_basis": {"pg_storage_gib_month_usd": price}, "savings_basis": "heap × proporción de tuplas muertas × precio de almacenamiento (cota superior)"},
        alternatives=[
            {"action": "AUTOVACUUM_TUNING", "description": "Si vuelve a hincharse, ajusta autovacuum_vacuum_scale_factor de la tabla: el origen suele ser un autovacuum que no alcanza."},
            {"action": "PG_REPACK", "description": "VACUUM FULL bloquea la tabla; pg_repack reorganiza sin bloquear las escrituras."}],
    )


RULES: list[Any] = [rule_qdrant_quantization, rule_qdrant_replication_nonprod, rule_pg_unused_index, rule_pg_table_bloat]
