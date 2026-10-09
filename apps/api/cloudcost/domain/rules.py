"""Motor de reglas determinísticas (primeras reglas: altamente confiables y explicables).

Las reglas NO ejecutan nada: producen `Finding` con evidencia que luego pasan por riesgo, políticas,
(opcionalmente) el asesor LLM, aprobación humana y finalmente un Pull Request.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Any, Callable

from .k8s import (
    DEFAULT_CPU_CORE_HOUR,
    DEFAULT_MEM_GIB_HOUR,
    MIB,
    format_cpu,
    format_memory,
    reserved_monthly_cost,
    round_up_cpu,
    round_up_memory,
)
from .models import COMPUTE_SERVICES, SNAPSHOT_SERVICES, VOLUME_SERVICES, NormalizedResource, is_protected, tf_type_for
from .pricing import (
    InstanceSpec,
    instance_monthly_cost,
    instance_spec,
    rds_monthly_cost,
    smaller_types,
    snapshot_monthly_cost,
    volume_monthly_cost,
)

ACTION_RESIZE = "RESIZE_INSTANCE"
ACTION_REMOVE = "REMOVE_RESOURCE"
ACTION_DELETE_VOLUME = "DELETE_VOLUME"
ACTION_DELETE_SNAPSHOT = "DELETE_SNAPSHOT"
ACTION_DELETE_DB = "DELETE_DB_INSTANCE"
ACTION_RIGHTSIZE_WORKLOAD = "RIGHTSIZE_WORKLOAD"


@dataclass(frozen=True)
class RuleConfig:
    # downsizing: CPU < 15% + memoria < 30% + ejecución prolongada
    cpu_downsize_threshold: float = 15.0
    mem_downsize_threshold: float = 30.0
    min_observation_days: int = 14
    require_memory_metric: bool = True
    max_downsize_steps: int = 2
    target_cpu_ceiling: float = 60.0       # uso medio proyectado máximo en el tipo destino
    target_mem_ceiling: float = 70.0
    target_peak_ceiling: float = 90.0      # pico de CPU proyectado máximo
    # instancia ociosa
    idle_cpu_threshold: float = 3.0
    idle_cpu_peak_threshold: float = 10.0
    idle_mem_ceiling: float = 50.0
    # almacenamiento
    orphan_volume_days: int = 14
    old_snapshot_days: int = 90
    # bases de datos RDS abandonadas: sin conexiones y CPU en reposo durante toda la ventana observada
    rds_idle_connections: float = 0.5      # conexiones medias (por debajo = nadie se conecta)
    rds_idle_cpu: float = 5.0
    rds_min_observation_days: int = 14
    # Kubernetes: requests de un contenedor frente a su uso real
    k8s_min_observation_days: int = 7
    k8s_cpu_headroom: float = 1.30         # CPU recomendada = p95 de uso × holgura
    k8s_mem_headroom: float = 1.25         # memoria recomendada = máximo observado × holgura (la memoria no se comprime)
    k8s_min_reduction: float = 0.30        # solo se propone si el recorte es de al menos este % de lo pedido
    k8s_min_cpu_cores: float = 0.05
    k8s_min_memory_mib: int = 64
    min_monthly_savings: float = 5.0
    # Si es True, no se propone apagar/reducir/eliminar nada cuyo costo sea solo una estimación por tabla de precios
    # (exige costo real: Cost Explorer o importación). Por defecto se propone igual, marcando la base del costo.
    require_verified_cost: bool = False

    @classmethod
    def from_overrides(cls, overrides: dict[str, Any] | None) -> "RuleConfig":
        """Aplica overrides por organización (tabla policies), ignorando claves desconocidas o de tipo inválido."""
        base = cls()
        if not overrides:
            return base
        valid: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in overrides:
                default = getattr(base, f.name)
                value = overrides[f.name]
                try:
                    valid[f.name] = type(default)(value)
                except (TypeError, ValueError):
                    continue
        return cls(**valid)


@dataclass
class Finding:
    rule_id: str
    action: str
    resource: NormalizedResource
    title: str
    summary: str
    current_monthly_cost: float
    projected_monthly_cost: float
    estimated_monthly_savings: float
    confidence: float
    destructive: bool
    params: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    stable_key: dict[str, Any] | None = None     # identidad estable si `params` cambia con cada escaneo (p. ej. valores recomendados)

    def dedupe_key(self, cloud_account_id: str) -> str:
        raw = json.dumps(
            [self.rule_id, cloud_account_id, self.resource.resource_id, self.action,
             self.params if self.stable_key is None else self.stable_key],
            sort_keys=True, default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- utilidades
def _current_cost(res: NormalizedResource) -> float:
    if res.monthly_cost and res.monthly_cost > 0:
        return float(res.monthly_cost)
    if res.service in COMPUTE_SERVICES:
        return instance_monthly_cost(res.instance_type) or 0.0
    if res.service in VOLUME_SERVICES:
        return volume_monthly_cost(res.volume_type, res.size_gb)
    if res.service in SNAPSHOT_SERVICES:
        return snapshot_monthly_cost(res.size_gb, res.provider)
    if res.service == "k8s_workload":
        a = res.attributes
        return reserved_monthly_cost(a.get("cpu_request") or 0.0, a.get("mem_request") or 0, int(a.get("replicas") or 1),
                                     a.get("cpu_hour_usd") or DEFAULT_CPU_CORE_HOUR, a.get("mem_gib_hour_usd") or DEFAULT_MEM_GIB_HOUR)
    if res.service == "rds":
        return rds_monthly_cost(res.instance_type, res.size_gb, bool(res.attributes.get("multi_az")))
    return 0.0


VERIFIED_COST_SOURCES = frozenset({"cost_explorer", "import"})


def _cost_unverified(res: NormalizedResource, cfg: RuleConfig) -> bool:
    return cfg.require_verified_cost and res.cost_source not in VERIFIED_COST_SOURCES


def _cost_basis(res: NormalizedResource) -> dict[str, Any]:
    """Origen del costo usado para calcular el ahorro: real (Cost Explorer/importación) o estimado por tabla de precios."""
    basis = dict(res.attributes.get("cost_basis") or {})
    basis.setdefault("source", res.cost_source)
    basis["verified"] = res.cost_source in VERIFIED_COST_SOURCES
    history = res.attributes.get("cost_history")
    if history:
        basis["history"] = {k: history[k] for k in ("scope", "tag_key", "tag_value", "monthly", "trend_pct") if k in history}
    return basis


def _clamp(value: float, lo: float = 0.0, hi: float = 0.97) -> float:
    return round(max(lo, min(hi, value)), 3)


@dataclass
class _Downsize:
    target: InstanceSpec
    cpu_pct: float
    mem_pct: float
    peak_pct: float | None


def _best_downsize(res: NormalizedResource, cfg: RuleConfig) -> _Downsize | None:
    """Tipo más pequeño (hasta max_downsize_steps) cuyo uso proyectado se mantiene bajo los techos configurados."""
    spec = instance_spec(res.instance_type)
    if not spec or res.cpu_avg is None or res.memory_avg is None:
        return None
    for cand in reversed(smaller_types(spec.name, cfg.max_downsize_steps)):   # del más pequeño al más cercano
        cpu = (spec.vcpu * res.cpu_avg / 100.0) / cand.vcpu * 100.0
        mem = (spec.memory_gib * res.memory_avg / 100.0) / cand.memory_gib * 100.0
        peak = (spec.vcpu * res.cpu_max / 100.0) / cand.vcpu * 100.0 if res.cpu_max is not None else None
        if cpu <= cfg.target_cpu_ceiling and mem <= cfg.target_mem_ceiling and (peak is None or peak <= cfg.target_peak_ceiling):
            return _Downsize(cand, round(cpu, 1), round(mem, 1), round(peak, 1) if peak is not None else None)
    return None


# --------------------------------------------------------------------------- reglas
def rule_ec2_downsize(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service not in COMPUTE_SERVICES or res.state != "running" or is_protected(res.tags):
        return None
    if res.cpu_avg is None or res.observation_days < cfg.min_observation_days:
        return None
    if res.memory_avg is None and cfg.require_memory_metric:
        return None
    if _cost_unverified(res, cfg):
        return None
    if res.cpu_avg >= cfg.cpu_downsize_threshold:
        return None
    if res.memory_avg is not None and res.memory_avg >= cfg.mem_downsize_threshold:
        return None
    best = _best_downsize(res, cfg)
    spec = instance_spec(res.instance_type)
    if not best or not spec:
        return None

    cost = _current_cost(res)
    savings = round(cost * (1 - best.target.hourly_usd / spec.hourly_usd), 2)
    if savings < cfg.min_monthly_savings:
        return None

    confidence = 0.70
    confidence += 0.10 if res.observation_days >= 30 else 0.05 if res.observation_days >= 21 else 0.0
    confidence += 0.08 if res.cpu_max is not None else 0.0
    confidence += 0.05 if res.cpu_avg <= cfg.cpu_downsize_threshold / 2 else 0.0
    confidence += 0.05 if res.iac_address else 0.0

    return Finding(
        rule_id="ec2_downsize", action=ACTION_RESIZE, resource=res, destructive=False,
        title=f"Reducir {res.name or res.resource_id}: {spec.name} → {best.target.name}",
        summary=(f"CPU media {res.cpu_avg:.1f}% y memoria media {res.memory_avg:.1f}% durante {res.observation_days} días. "
                 f"Con {best.target.name} el uso proyectado sería CPU {best.cpu_pct}% / memoria {best.mem_pct}%."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=round(cost - savings, 2),
        estimated_monthly_savings=savings, confidence=_clamp(confidence),
        params={"current_instance_type": spec.name, "target_instance_type": best.target.name},
        evidence={
            "cpu_avg": res.cpu_avg, "cpu_max": res.cpu_max, "memory_avg": res.memory_avg,
            "observation_days": res.observation_days, "environment": res.environment,
            "current_vcpu": spec.vcpu, "current_memory_gib": spec.memory_gib,
            "target_vcpu": best.target.vcpu, "target_memory_gib": best.target.memory_gib,
            "cost_basis": _cost_basis(res),
            "projected_cpu_pct": best.cpu_pct, "projected_mem_pct": best.mem_pct, "projected_peak_pct": best.peak_pct,
            "thresholds": {"cpu": cfg.cpu_downsize_threshold, "memory": cfg.mem_downsize_threshold,
                           "target_cpu_ceiling": cfg.target_cpu_ceiling, "target_mem_ceiling": cfg.target_mem_ceiling},
        },
    )


def rule_ec2_idle(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service not in COMPUTE_SERVICES or res.state != "running" or is_protected(res.tags):
        return None
    if res.cpu_avg is None or res.observation_days < cfg.min_observation_days:
        return None
    if res.cpu_avg >= cfg.idle_cpu_threshold:
        return None
    if res.cpu_max is not None and res.cpu_max >= cfg.idle_cpu_peak_threshold:
        return None
    if res.memory_avg is not None and res.memory_avg >= cfg.idle_mem_ceiling:
        return None
    if _cost_unverified(res, cfg):
        return None
    cost = _current_cost(res)
    if cost < cfg.min_monthly_savings:
        return None

    confidence = 0.75
    confidence += 0.10 if res.observation_days >= 30 else 0.0
    confidence += 0.05 if res.cpu_max is not None else 0.0
    confidence += 0.05 if res.iac_address else 0.0

    alternatives: list[dict[str, Any]] = []
    alt = _best_downsize(res, cfg)
    spec = instance_spec(res.instance_type)
    if alt and spec:
        alt_savings = round(cost * (1 - alt.target.hourly_usd / spec.hourly_usd), 2)
        alternatives.append({"action": ACTION_RESIZE, "target_instance_type": alt.target.name,
                             "estimated_monthly_savings": alt_savings,
                             "description": "Reducir el tamaño en lugar de eliminar la instancia."})
    return Finding(
        rule_id="ec2_idle", action=ACTION_REMOVE, resource=res, destructive=True,
        title=f"Eliminar instancia ociosa {res.name or res.resource_id}",
        summary=(f"CPU media {res.cpu_avg:.1f}%"
                 + (f" (pico {res.cpu_max:.1f}%)" if res.cpu_max is not None else "")
                 + f" durante {res.observation_days} días: la instancia parece ociosa."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0,
        estimated_monthly_savings=round(cost, 2), confidence=_clamp(confidence),
        params={"resource_type": tf_type_for(res), "resource_id": res.resource_id},
        evidence={"cpu_avg": res.cpu_avg, "cpu_max": res.cpu_max, "memory_avg": res.memory_avg,
                  "observation_days": res.observation_days, "environment": res.environment,
                  "instance_type": res.instance_type, "cost_basis": _cost_basis(res)},
        alternatives=alternatives,
    )


def _volume_origin(tags: dict[str, str]) -> dict[str, str] | None:
    """De dónde salió un volumen huérfano, según las etiquetas que dejan CloudFormation y el driver CSI de Kubernetes."""
    stack = tags.get("aws:cloudformation:stack-name")
    if stack:
        return {"kind": "cloudformation", "ref": stack,
                "text": f"Lo creó la pila de CloudFormation '{stack}': si la pila ya no se usa, elimínala en lugar de borrar volúmenes sueltos."}
    pvc = tags.get("kubernetes.io/created-for/pvc/name")
    if pvc:
        ns = tags.get("kubernetes.io/created-for/pvc/namespace")
        ref = f"{ns}/{pvc}" if ns else pvc
        return {"kind": "kubernetes_pvc", "ref": ref,
                "text": f"Corresponde al PersistentVolumeClaim '{ref}' de Kubernetes (probablemente ya eliminado): limpia el PV con kubectl."}
    return None


def rule_ebs_orphan(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service not in VOLUME_SERVICES or res.attached is not False or res.state != "available" or is_protected(res.tags):
        return None
    if res.unattached_days is None or res.unattached_days < cfg.orphan_volume_days or _cost_unverified(res, cfg):
        return None
    cost = _current_cost(res)
    if cost < cfg.min_monthly_savings:
        return None
    confidence = 0.80
    confidence += 0.10 if res.unattached_days >= 2 * cfg.orphan_volume_days else 0.0
    confidence += 0.03 if res.iac_address else 0.0
    origin = _volume_origin(res.tags)
    return Finding(
        rule_id="ebs_orphan", action=ACTION_DELETE_VOLUME, resource=res, destructive=True,
        title=f"Eliminar volumen huérfano {res.name or res.resource_id}",
        summary=(f"Volumen {res.volume_type or ''} de {res.size_gb:g} GB sin attachment desde hace {res.unattached_days} días "
                 f"(umbral {cfg.orphan_volume_days})." + (f" {origin['text']}" if origin else "")),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0,
        estimated_monthly_savings=round(cost, 2), confidence=_clamp(confidence),
        params={"resource_type": tf_type_for(res), "resource_id": res.resource_id},
        evidence={"unattached_days": res.unattached_days, "size_gb": res.size_gb, "volume_type": res.volume_type,
                  "environment": res.environment, "threshold_days": cfg.orphan_volume_days,
                  "cost_basis": _cost_basis(res),
                  **({"origin": origin["kind"], "origin_ref": origin["ref"]} if origin else {})},
        alternatives=[{"action": "SNAPSHOT_THEN_DELETE",
                       "description": "Tomar un snapshot final antes de eliminar, si los datos pudieran necesitarse."}],
    )


def rule_snapshot_old(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service not in SNAPSHOT_SERVICES or is_protected(res.tags):
        return None
    if res.attributes.get("managed_by") or res.attributes.get("ami_ids"):
        return None          # gestionado por un servicio de copias (AWS Backup/DLM, Azure Backup, programación de GCP) o usado por una imagen: no es seguro proponerlo
    if res.age_days is None or res.age_days < cfg.old_snapshot_days or _cost_unverified(res, cfg):
        return None
    cost = _current_cost(res)
    if cost < cfg.min_monthly_savings:
        return None
    confidence = 0.85
    confidence += 0.08 if res.age_days >= 2 * cfg.old_snapshot_days else 0.0
    confidence += 0.03 if res.iac_address else 0.0
    return Finding(
        rule_id="snapshot_old", action=ACTION_DELETE_SNAPSHOT, resource=res, destructive=True,
        title=f"Eliminar snapshot antiguo {res.name or res.resource_id}",
        summary=(f"Snapshot de {res.size_gb:g} GB con {res.age_days} días de antigüedad (umbral {cfg.old_snapshot_days}), "
                 "sin imagen (AMI/imagen) asociada ni gestión por un servicio de copias automáticas."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0,
        estimated_monthly_savings=round(cost, 2), confidence=_clamp(confidence),
        params={"resource_type": tf_type_for(res), "resource_id": res.resource_id},
        evidence={"age_days": res.age_days, "size_gb": res.size_gb, "environment": res.environment,
                  "savings_basis": "cota superior (snapshots incrementales)", "cost_basis": _cost_basis(res)},
    )


def rule_rds_idle(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Base de datos RDS abandonada: sin conexiones ni CPU durante toda la ventana. Siempre destructiva; el riesgo lo fija risk.py (ALTO)."""
    if res.service != "rds" or res.state != "available" or is_protected(res.tags):
        return None
    conns = res.attributes.get("connections_avg")
    if conns is None or res.cpu_avg is None or res.observation_days < cfg.rds_min_observation_days:
        return None
    peak = res.attributes.get("connections_max")
    if conns > cfg.rds_idle_connections or res.cpu_avg > cfg.rds_idle_cpu or (peak is not None and peak > 2):
        return None
    cost = _current_cost(res)
    if cost < cfg.min_monthly_savings:
        return None
    confidence = 0.78
    confidence += 0.10 if res.observation_days >= 30 else 0.0
    confidence += 0.04 if peak is not None else 0.0
    confidence += 0.03 if res.iac_address else 0.0
    confidence -= 0.10 if res.attributes.get("deletion_protection") else 0.0      # alguien lo protegió a propósito: menos seguro
    confidence -= 0.05 if res.attributes.get("read_replicas") else 0.0
    backup = res.attributes.get("backup_retention_days")
    return Finding(
        rule_id="rds_idle", action=ACTION_DELETE_DB, resource=res, destructive=True,
        title=f"Eliminar base de datos abandonada {res.name or res.resource_id}",
        summary=(f"{res.instance_type or 'RDS'} ({res.attributes.get('engine', 'motor desconocido')}) sin conexiones: media {conns:g}"
                 + (f", máximo {peak:g}" if peak is not None else "")
                 + f", CPU media {res.cpu_avg:.1f}% durante {res.observation_days} días."
                 + (" Tiene protección contra borrado: habrá que desactivarla antes de aplicar el cambio." if res.attributes.get("deletion_protection") else "")),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0,
        estimated_monthly_savings=round(cost, 2), confidence=_clamp(confidence),
        params={"resource_type": "aws_db_instance", "resource_id": res.resource_id},
        evidence={"connections_avg": conns, "connections_max": peak, "cpu_avg": res.cpu_avg, "observation_days": res.observation_days,
                  "environment": res.environment, "engine": res.attributes.get("engine"), "storage_gb": res.size_gb,
                  "multi_az": bool(res.attributes.get("multi_az")), "backup_retention_days": backup,
                  "deletion_protection": bool(res.attributes.get("deletion_protection"))},
        alternatives=[
            {"action": "SNAPSHOT_THEN_DELETE", "description": "Tomar un snapshot final antes de eliminar (en Terraform: skip_final_snapshot = false y final_snapshot_identifier)."},
            {"action": "STOP_TEMPORARILY", "description": "Detenerla: no cobra cómputo, pero AWS la reinicia sola a los 7 días y el almacenamiento se sigue cobrando."},
        ],
    )


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(1e-9, 1e-6 * abs(b))


def rule_k8s_overprovisioned(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Contenedor que reserva (requests) mucho más de lo que usa. No elimina nada: baja requests (y límites iguales a ellos)."""
    if res.service != "k8s_workload" or res.state != "running" or is_protected(res.tags):
        return None
    a = res.attributes
    if res.observation_days < cfg.k8s_min_observation_days:
        return None
    cpu_req, mem_req = a.get("cpu_request"), a.get("mem_request")
    cpu_p95, cpu_max, mem_max = a.get("cpu_p95_cores"), a.get("cpu_max_cores"), a.get("mem_max_bytes")
    if not cpu_req or not mem_req or cpu_p95 is None or cpu_max is None or mem_max is None:
        return None
    replicas = max(1, int(a.get("replicas") or 1))

    new_cpu = new_mem = None
    if not a.get("hpa"):                           # con HPA los requests fijan el % de uso que escala: cambiarlos cambia las réplicas
        want = round_up_cpu(max(cpu_p95 * cfg.k8s_cpu_headroom, cpu_max * 0.8, cfg.k8s_min_cpu_cores))
        if want <= cpu_req * (1 - cfg.k8s_min_reduction):
            new_cpu = want
    if not a.get("oom_killed"):                    # un contenedor que ya murió por falta de memoria no se recorta
        want_mem = round_up_memory(max(mem_max * cfg.k8s_mem_headroom, cfg.k8s_min_memory_mib * MIB))
        if want_mem <= mem_req * (1 - cfg.k8s_min_reduction):
            new_mem = want_mem
    if new_cpu is None and new_mem is None:
        return None

    cpu_hour = a.get("cpu_hour_usd") or DEFAULT_CPU_CORE_HOUR
    mem_hour = a.get("mem_gib_hour_usd") or DEFAULT_MEM_GIB_HOUR
    cost = _current_cost(res)
    savings = reserved_monthly_cost(cpu_req - new_cpu if new_cpu is not None else 0.0,
                                    mem_req - new_mem if new_mem is not None else 0, replicas, cpu_hour, mem_hour)
    if savings < cfg.min_monthly_savings:
        return None

    cpu_lim, mem_lim = a.get("cpu_limit"), a.get("mem_limit")
    target: dict[str, str] = {}
    limits: dict[str, str] = {}                     # solo cuando límite == request (QoS Guaranteed): se mantiene la igualdad
    current = {"cpu": format_cpu(cpu_req), "memory": format_memory(mem_req)}
    if new_cpu is not None:
        target["cpu"] = format_cpu(new_cpu)
        if _same(cpu_lim, cpu_req):
            limits["cpu"] = target["cpu"]
    if new_mem is not None:
        target["memory"] = format_memory(new_mem)
        if _same(mem_lim, mem_req):
            limits["memory"] = target["memory"]

    confidence = 0.70
    confidence += 0.08 if res.observation_days >= 14 else 0.0
    confidence += 0.05 if res.iac_address else 0.0
    confidence += 0.04 if replicas >= 2 else -0.05
    confidence -= 0.05 if cpu_max > (new_cpu or cpu_req) else 0.0       # el pico supera lo recomendado: posible estrangulamiento

    parts = []
    if new_cpu is not None:
        parts.append(f"CPU: pide {current['cpu']}, usa de media {a.get('cpu_avg_cores', 0) * 1000:.0f}m (p95 {cpu_p95 * 1000:.0f}m, "
                     f"máx {cpu_max * 1000:.0f}m) → {target['cpu']}")
    if new_mem is not None:
        parts.append(f"Memoria: pide {current['memory']}, máximo observado {format_memory(mem_max)} → {target['memory']}")
    notes = []
    if a.get("hpa") and new_cpu is None:
        notes.append("tiene HPA: la CPU no se toca")
    if a.get("oom_killed") and new_mem is None:
        notes.append("hubo reinicios por OOM: la memoria no se toca")
    ident = {"namespace": a.get("namespace"), "kind": a.get("kind"), "workload": a.get("workload"), "container": a.get("container")}
    label = f"{a.get('namespace')}/{a.get('workload')} ({a.get('container')})"
    return Finding(
        rule_id="k8s_overprovisioned", action=ACTION_RIGHTSIZE_WORKLOAD, resource=res, destructive=False,
        title=f"Ajustar requests de {label}",
        summary=(". ".join(parts) + f". {replicas} réplica{'s' if replicas != 1 else ''}, {res.observation_days} días de datos."
                 + (f" Nota: {'; '.join(notes)}." if notes else "")),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=round(max(0.0, cost - savings), 2),
        estimated_monthly_savings=savings, confidence=_clamp(confidence),
        params={**ident, "current": current, "target": target, "limits": limits,
                "current_limits": {k: v for k, v in (("cpu", format_cpu(cpu_lim) if cpu_lim else None),
                                                      ("memory", format_memory(mem_lim) if mem_lim else None)) if v}},
        stable_key=ident,
        evidence={"environment": res.environment, "replicas": replicas, "observation_days": res.observation_days,
                  "cpu_request_cores": cpu_req, "cpu_avg_cores": a.get("cpu_avg_cores"), "cpu_p95_cores": cpu_p95, "cpu_max_cores": cpu_max,
                  "memory_request_mib": round(mem_req / MIB, 1), "memory_avg_mib": round((a.get("mem_avg_bytes") or 0) / MIB, 1),
                  "memory_max_mib": round(mem_max / MIB, 1), "hpa": bool(a.get("hpa")), "oom_killed": bool(a.get("oom_killed")),
                  "cpu_limit_cores": cpu_lim, "memory_limit_mib": round(mem_lim / MIB, 1) if mem_lim else None,
                  "headroom": {"cpu": cfg.k8s_cpu_headroom, "memory": cfg.k8s_mem_headroom},
                  "price_basis": {"cpu_core_hour_usd": cpu_hour, "mem_gib_hour_usd": mem_hour}, "helm": a.get("helm") or None,
                  "savings_basis": "costo de lo reservado (requests × réplicas) con precios por vCPU y GiB; no es la factura del nodo"},
        alternatives=[{"action": "VPA_RECOMMENDATION", "description": "Dejar que un VerticalPodAutoscaler en modo «Off» calcule los requests y compararlos con esta propuesta."}],
    )


Rule = Callable[[NormalizedResource, RuleConfig], "Finding | None"]
RULES: list[Rule] = [rule_ec2_idle, rule_ec2_downsize, rule_ebs_orphan, rule_snapshot_old, rule_rds_idle, rule_k8s_overprovisioned]


def evaluate_resource(res: NormalizedResource, cfg: RuleConfig) -> list[Finding]:
    findings = [f for rule in RULES if (f := rule(res, cfg))]
    # Resolución de conflictos: una instancia ociosa ya cubre el resize (queda como alternativa).
    if any(f.rule_id == "ec2_idle" for f in findings):
        findings = [f for f in findings if f.rule_id != "ec2_downsize"]
    return findings


def evaluate_all(resources: list[NormalizedResource], cfg: RuleConfig) -> list[Finding]:
    out: list[Finding] = []
    for res in resources:
        out.extend(evaluate_resource(res, cfg))
    return out
