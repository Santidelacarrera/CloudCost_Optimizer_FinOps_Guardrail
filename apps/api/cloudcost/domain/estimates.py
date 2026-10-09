"""Fundamento de cada cifra de ahorro: fórmula, entradas, coste de referencia y supuestos.

Una recomendación sin fundamento es una opinión. Aquí cada regla tiene una fórmula con identificador y versión (`ec2_downsize.v1`),
declarada junto a sus supuestos y límites, y cada hallazgo guarda en `evidence["estimate"]` los valores concretos con los que se
aplicó. El documento humano equivalente es docs/savings-methodology.md; una prueba comprueba que ambos coinciden.

El ahorro que se calcula aquí es SIEMPRE «estimado»: lo que costaría menos si el cambio se aplicara y todo lo demás siguiera igual.
«Aprobado» es esa misma cifra congelada cuando las personas autorizadas la aprueban; «observado» es lo que se mide después del
despliegue (ver services/savings_measurement.py). Son tres números distintos y nunca se mezclan.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from .cost_quality import PRICE_TABLE_RATIO_HIGH, PRICE_TABLE_RATIO_LOW
from .models import COMPUTE_SERVICES, SNAPSHOT_SERVICES, VOLUME_SERVICES, NormalizedResource
from .pricing import instance_monthly_cost, instance_spec, rds_monthly_cost, snapshot_monthly_cost, volume_monthly_cost

if TYPE_CHECKING:  # pragma: no cover
    from .rules import Finding

# Tipos de ahorro:
EXACT = "exact"                   # se deja de pagar íntegro el coste de referencia
RATIO = "ratio"                   # coste de referencia × (1 − proporción de precios)
UPPER_BOUND = "upper_bound"       # cota superior: el ahorro real puede ser menor
RESERVED = "reserved"             # coste de lo reservado (no es la factura del nodo)

COMMON_ASSUMPTIONS = (
    "Importes en USD por mes (30,4375 días); precios bajo demanda, sin Reserved Instances ni Savings Plans, créditos ni impuestos.",
    "El resto de la infraestructura y el uso se mantienen igual tras aplicar el cambio.",
)


@dataclass(frozen=True)
class Formula:
    rule_id: str
    version: int
    kind: str
    expression: str
    description: str
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def id(self) -> str:
        return f"{self.rule_id}.v{self.version}"


FORMULAS: dict[str, Formula] = {f.rule_id: f for f in (
    Formula(
        "ec2_downsize", 1, RATIO,
        "ahorro_mensual = coste_mensual_actual × (1 − precio_hora(tipo_destino) / precio_hora(tipo_actual))",
        "Reducir hasta 2 tamaños dentro de la misma familia si el uso proyectado en el tipo destino queda bajo los techos configurados.",
        ("El coste mensual actual es el observado (Cost Explorer, últimos 14 días) o, si no hay, el de la tabla de precios.",
         "El ahorro escala con la proporción de precios bajo demanda de ambos tipos (misma región y sistema operativo), no con el uso.",
         "La instancia seguirá funcionando el mismo número de horas.",
         "El uso proyectado (CPU, memoria, pico) se calcula multiplicando el uso observado por vCPU/GiB actuales entre los del destino."),
        ("Si la instancia tiene Reserved Instances o Savings Plans el ahorro efectivo es menor (el compromiso se sigue pagando).",
         "No incluye EBS, transferencia ni licencias; el cambio puede requerir reinicio."),
    ),
    Formula(
        "ec2_idle", 1, EXACT,
        "ahorro_mensual = coste_mensual_actual de la instancia",
        "Eliminar una instancia en ejecución con CPU media y pico mínimos durante toda la ventana observada.",
        ("Al eliminar la instancia deja de facturarse su cómputo; sus volúmenes EBS, IP elásticas y transferencia se evalúan aparte y no se suman.",
         "La instancia no sirve tráfico que las métricas de CPU no reflejen (p. ej. colas, tareas por lotes poco frecuentes)."),
        ("Con Reserved Instances o Savings Plans el compromiso se sigue pagando: el ahorro real puede ser cero.",
         "Es una acción destructiva: la estimación no mide el coste de reponerla si se equivocara el diagnóstico."),
    ),
    Formula(
        "ebs_orphan", 1, EXACT,
        "ahorro_mensual = tamaño_GB × precio_GB_mes(tipo_de_volumen)  [o el coste real del volumen si lo hay]",
        "Eliminar un volumen sin adjuntar durante al menos el número de días del umbral.",
        ("El volumen se elimina sin snapshot final; si se toma uno, el ahorro disminuye en el coste de ese snapshot.",
         "El tiempo sin adjuntar se toma del último DetachVolume en CloudTrail (90 días) o de la primera vez que el servicio lo vio huérfano."),
        ("Un volumen ya sin adjuntar puede ser una pieza de recuperación o de un despliegue en curso.",),
    ),
    Formula(
        "snapshot_old", 1, UPPER_BOUND,
        "ahorro_mensual ≤ tamaño_GB × precio_GB_mes(snapshot)",
        "Eliminar un snapshot antiguo, sin imagen asociada y no gestionado por un servicio de copias.",
        ("Se calcula sobre el tamaño del volumen, no sobre los bloques realmente almacenados.",),
        ("Cota SUPERIOR: los snapshots son incrementales y comparten bloques con los siguientes; borrar uno suele liberar menos.",),
    ),
    Formula(
        "rds_idle", 1, EXACT,
        "ahorro_mensual = (precio_hora(clase) × 730 + precio_GB_mes × almacenamiento_GB) × (2 si Multi-AZ)",
        "Eliminar una base de datos RDS sin conexiones y con CPU en reposo toda la ventana observada.",
        ("Precios de RDS MySQL/PostgreSQL bajo demanda; clases desconocidas se valoran solo por almacenamiento.",
         "No se suman copias de seguridad automáticas ni snapshots manuales."),
        ("Una base «sin conexiones» puede ser un entorno dormido que se usa a fin de mes o en una recuperación.",),
    ),
    Formula(
        "k8s_overprovisioned", 1, RESERVED,
        "ahorro_mensual = (Δcpu_cores × precio_vCPU_hora + Δmemoria_GiB × precio_GiB_hora) × 730 × réplicas",
        "Bajar los requests de un contenedor hasta el p95 de uso × holgura (CPU) o el máximo × holgura (memoria).",
        ("Se valora lo reservado (requests × réplicas) con precios por vCPU y GiB; no es la factura del nodo.",
         "La holgura (CPU 1,30; memoria 1,25) cubre la variabilidad observada en la ventana."),
        ("El ahorro solo se materializa si el planificador/autoscaler de nodos retira capacidad.",),
    ),
)}


def table_cost(res: NormalizedResource) -> float:
    """Coste mensual según la tabla de precios para el mismo recurso (0 si no hay tabla para él)."""
    if res.service in COMPUTE_SERVICES:
        return instance_monthly_cost(res.instance_type) or 0.0
    if res.service in VOLUME_SERVICES:
        return volume_monthly_cost(res.volume_type, res.size_gb)
    if res.service in SNAPSHOT_SERVICES:
        return snapshot_monthly_cost(res.size_gb, res.provider)
    if res.service == "rds":
        return rds_monthly_cost(res.instance_type, res.size_gb, bool(res.attributes.get("multi_az")))
    return 0.0


def price_table_check(res: NormalizedResource, reference_cost: float) -> dict[str, Any] | None:
    """Compara el coste de referencia con el de la tabla de precios. Un cociente fuera de [0,3 ; 3] se marca como atípico."""
    table = table_cost(res)
    if res.cost_source in ("estimate", "estimate_upper_bound") or table <= 0 or reference_cost <= 0:
        return None
    ratio = reference_cost / table
    return {"table_monthly_cost": round(table, 2), "ratio": round(ratio, 2),
            "outlier": ratio > PRICE_TABLE_RATIO_HIGH or ratio < PRICE_TABLE_RATIO_LOW}


def _inputs(finding: "Finding") -> dict[str, Any]:
    p, cost = finding.params, finding.current_monthly_cost
    if finding.rule_id == "ec2_downsize":
        cur, tgt = instance_spec(p.get("current_instance_type")), instance_spec(p.get("target_instance_type"))
        if cur and tgt:
            return {"reference_monthly_cost": cost, "hourly_price_current": cur.hourly_usd, "hourly_price_target": tgt.hourly_usd,
                    "price_ratio": round(tgt.hourly_usd / cur.hourly_usd, 4)}
    if finding.rule_id in ("ec2_idle", "ebs_orphan", "snapshot_old", "rds_idle"):
        return {"reference_monthly_cost": cost}
    if finding.rule_id == "k8s_overprovisioned":
        return {"reference_monthly_cost": cost, "price_basis": finding.evidence.get("price_basis"),
                "replicas": finding.evidence.get("replicas")}
    return {"reference_monthly_cost": cost}


def attach_estimate(finding: "Finding", res: NormalizedResource, *, as_of: date | None = None) -> dict[str, Any]:
    """Escribe `evidence["estimate"]` con la fórmula aplicada, sus entradas, el coste de referencia y los supuestos. Devuelve el bloque."""
    formula = FORMULAS[finding.rule_id]
    basis = dict(finding.evidence.get("cost_basis") or {})
    reference = {
        "monthly_cost": round(finding.current_monthly_cost, 2), "source": basis.get("source", res.cost_source),
        "verified": bool(basis.get("verified")), "window_days": basis.get("window_days"),
        "window_start": basis.get("window_start"), "window_end": basis.get("window_end"),
        "days_with_data": basis.get("days_with_data"), "quality_flags": list(basis.get("quality_flags") or []),
        "as_of": basis.get("as_of") or (as_of or date.today()).isoformat(),
    }
    check = price_table_check(res, finding.current_monthly_cost)
    if check:
        reference["vs_price_table"] = check
    assumptions = list(COMMON_ASSUMPTIONS) + list(formula.assumptions)
    if not reference["verified"]:
        assumptions.append("El coste de referencia es ESTIMADO con la tabla de precios (no hay coste real disponible): confírmalo con tu factura.")
    if reference["quality_flags"]:
        assumptions.append("La serie de costes tiene señales de calidad (" + ", ".join(reference["quality_flags"])
                           + "): se usó el valor prudente descrito en docs/savings-methodology.md.")
    block = {
        "formula_id": formula.id, "kind": formula.kind, "expression": formula.expression,
        "inputs": _inputs(finding), "reference": reference, "assumptions": assumptions, "limitations": list(formula.limitations),
        "result": {"estimated_monthly_savings": round(finding.estimated_monthly_savings, 2),
                   "projected_monthly_cost": round(finding.projected_monthly_cost, 2),
                   "annualized": round(finding.estimated_monthly_savings * 12, 2)},
    }
    finding.evidence["estimate"] = block
    return block
