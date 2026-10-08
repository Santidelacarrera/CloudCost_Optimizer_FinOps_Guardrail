"""Motor de reglas determinísticas (primeras reglas: altamente confiables y explicables).

Las reglas NO ejecutan nada: producen `Finding` con evidencia que luego pasan por riesgo, políticas,
(opcionalmente) el asesor LLM, aprobación humana y finalmente un Pull Request.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Any, Callable

from .models import NormalizedResource, is_protected
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
    min_monthly_savings: float = 5.0
    # bases de datos: sin conexiones de aplicación durante la ventana de observación
    rds_idle_max_connections: float = 2.0     # tolera agentes de monitoreo/backups
    rds_idle_cpu_threshold: float = 5.0
    rds_min_observation_days: int = 14
    # costos: exigir costo real (Cost Explorer/CUR/importado) antes de proponer apagar o reducir un recurso
    require_real_cost: bool = False

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
                    if isinstance(default, bool):          # bool("false") es True: se interpreta el texto explícitamente
                        text = str(value).strip().lower()
                        if text not in ("true", "false", "1", "0"):
                            continue
                        valid[f.name] = text in ("true", "1")
                    else:
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

    def dedupe_key(self, cloud_account_id: str) -> str:
        raw = json.dumps(
            [self.rule_id, cloud_account_id, self.resource.resource_id, self.action, self.params],
            sort_keys=True, default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- utilidades
def _current_cost(res: NormalizedResource) -> float:
    if res.monthly_cost and res.monthly_cost > 0:
        return float(res.monthly_cost)
    if res.service == "ec2":
        return instance_monthly_cost(res.instance_type) or 0.0
    if res.service == "ebs":
        return volume_monthly_cost(res.volume_type, res.size_gb)
    if res.service == "ebs_snapshot":
        return snapshot_monthly_cost(res.size_gb)
    if res.service == "rds":
        return rds_monthly_cost(res.instance_type, res.size_gb, bool(res.attributes.get("multi_az")))
    return 0.0


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
    if res.service != "ec2" or res.state != "running" or is_protected(res.tags):
        return None
    if res.cpu_avg is None or res.observation_days < cfg.min_observation_days:
        return None
    if res.memory_avg is None and cfg.require_memory_metric:
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
            "projected_cpu_pct": best.cpu_pct, "projected_mem_pct": best.mem_pct, "projected_peak_pct": best.peak_pct,
            "thresholds": {"cpu": cfg.cpu_downsize_threshold, "memory": cfg.mem_downsize_threshold,
                           "target_cpu_ceiling": cfg.target_cpu_ceiling, "target_mem_ceiling": cfg.target_mem_ceiling},
        },
    )


def rule_ec2_idle(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service != "ec2" or res.state != "running" or is_protected(res.tags):
        return None
    if res.cpu_avg is None or res.observation_days < cfg.min_observation_days:
        return None
    if res.cpu_avg >= cfg.idle_cpu_threshold:
        return None
    if res.cpu_max is not None and res.cpu_max >= cfg.idle_cpu_peak_threshold:
        return None
    if res.memory_avg is not None and res.memory_avg >= cfg.idle_mem_ceiling:
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
        params={"resource_type": "aws_instance", "resource_id": res.resource_id},
        evidence={"cpu_avg": res.cpu_avg, "cpu_max": res.cpu_max, "memory_avg": res.memory_avg,
                  "observation_days": res.observation_days, "environment": res.environment,
                  "instance_type": res.instance_type},
        alternatives=alternatives,
    )


def _volume_origin(tags: dict[str, str]) -> dict[str, str] | None:
    """De dónde salió un volumen huérfano, según las etiquetas que dejan CloudFormation y el driver CSI de Kubernetes."""
    stack = tags.get("aws:cloudformation:stack-name")
    if stack:
        return {"kind": "cloudformation", "ref": stack,
                "text": f"Lo creó la pila de CloudFormation '{stack}': si la pila ya no se usa, elimínala en lugar de borrar volúmenes sueltos."}
    ns = tags.get("kubernetes.io/created-for/pvc/namespace")
    if ns:
        return {"kind": "kubernetes_pvc", "ref": ns,
                "text": f"Corresponde a un PersistentVolumeClaim del namespace '{ns}' (probablemente ya eliminado): limpia el PV con kubectl."}
    return None


def rule_ebs_orphan(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service != "ebs" or res.attached is not False or res.state != "available" or is_protected(res.tags):
        return None
    if res.unattached_days is None or res.unattached_days < cfg.orphan_volume_days:
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
        params={"resource_type": "aws_ebs_volume", "resource_id": res.resource_id},
        evidence={"unattached_days": res.unattached_days, "size_gb": res.size_gb, "volume_type": res.volume_type,
                  "environment": res.environment, "threshold_days": cfg.orphan_volume_days,
                  **({"origin": origin["kind"], "origin_ref": origin["ref"]} if origin else {})},
        alternatives=[{"action": "SNAPSHOT_THEN_DELETE",
                       "description": "Tomar un snapshot final antes de eliminar, si los datos pudieran necesitarse."}],
    )


def rule_snapshot_old(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    if res.service != "ebs_snapshot" or is_protected(res.tags):
        return None
    if res.attributes.get("managed_by") or res.attributes.get("ami_ids"):
        return None          # gestionado por AWS Backup/DLM o usado por una AMI: no es seguro proponerlo
    if res.age_days is None or res.age_days < cfg.old_snapshot_days:
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
                 "sin AMI asociada ni gestión por AWS Backup/DLM."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0,
        estimated_monthly_savings=round(cost, 2), confidence=_clamp(confidence),
        params={"resource_type": "aws_ebs_snapshot", "resource_id": res.resource_id},
        evidence={"age_days": res.age_days, "size_gb": res.size_gb, "environment": res.environment,
                  "savings_basis": "cota superior (snapshots incrementales)"},
    )


def rule_rds_idle(res: NormalizedResource, cfg: RuleConfig) -> Finding | None:
    """Instancia/clúster RDS sin conexiones de aplicación: típico tras una migración que dejó la base anterior encendida."""
    if res.service != "rds" or res.state != "available" or is_protected(res.tags):
        return None
    conn_max = res.attributes.get("connections_max")
    if conn_max is None or res.cpu_avg is None or res.observation_days < cfg.rds_min_observation_days:
        return None                      # sin métrica de conexiones no se infiere abandono
    if conn_max > cfg.rds_idle_max_connections or res.cpu_avg >= cfg.rds_idle_cpu_threshold:
        return None
    cost = _current_cost(res)
    if cost < cfg.min_monthly_savings:
        return None
    cluster = res.attributes.get("cluster_id")
    confidence = 0.78
    confidence += 0.08 if res.observation_days >= 30 else 0.0
    confidence += 0.05 if conn_max == 0 else 0.0
    confidence += 0.03 if res.iac_address else 0.0
    where = f" del clúster {cluster}" if cluster else ""
    return Finding(
        rule_id="rds_idle", action=ACTION_REMOVE, resource=res, destructive=True,
        title=f"Eliminar base de datos abandonada {res.name or res.resource_id}",
        summary=(f"Instancia {res.instance_type}{where} sin conexiones (máximo {conn_max:g}) y CPU media {res.cpu_avg:.1f}% "
                 f"durante {res.observation_days} días. Cuesta USD {cost:,.2f}/mes entre cómputo y almacenamiento."),
        current_monthly_cost=round(cost, 2), projected_monthly_cost=0.0,
        estimated_monthly_savings=round(cost, 2), confidence=_clamp(confidence),
        params={"resource_type": "aws_db_instance", "resource_id": res.resource_id},
        evidence={"connections_max": conn_max, "connections_avg": res.attributes.get("connections_avg"),
                  "cpu_avg": res.cpu_avg, "observation_days": res.observation_days, "environment": res.environment,
                  "instance_class": res.instance_type, "storage_gb": res.size_gb, "engine": res.attributes.get("engine"),
                  "cluster_id": cluster},
        alternatives=[
            {"action": "SNAPSHOT_THEN_DELETE",
             "description": "Conservar un snapshot final (final_snapshot_identifier) y eliminar la instancia."},
            {"action": "STOP_TEMPORARILY",
             "description": "Detener la instancia (RDS la reinicia sola a los 7 días): útil si dudas de que nadie la use."}],
    )


Rule = Callable[[NormalizedResource, RuleConfig], "Finding | None"]
RULES: list[Rule] = [rule_ec2_idle, rule_ec2_downsize, rule_ebs_orphan, rule_snapshot_old, rule_rds_idle]


def evaluate_resource(res: NormalizedResource, cfg: RuleConfig) -> list[Finding]:
    findings = [f for rule in RULES if (f := rule(res, cfg))]
    # Resolución de conflictos: una instancia ociosa ya cubre el resize (queda como alternativa).
    if any(f.rule_id == "ec2_idle" for f in findings):
        findings = [f for f in findings if f.rule_id != "ec2_downsize"]
    return findings


REAL_COST_SOURCES = frozenset({"cost_explorer", "cost_explorer_tag", "cur", "import"})


def has_real_cost(res: NormalizedResource) -> bool:
    return res.cost_source in REAL_COST_SOURCES


def cost_basis(res: NormalizedResource) -> dict[str, Any]:
    """Origen del costo que sustenta el ahorro propuesto; viaja en la evidencia para que quien aprueba lo vea."""
    basis: dict[str, Any] = {"source": res.cost_source, "verified": has_real_cost(res), "monthly_cost": round(_current_cost(res), 2)}
    if "cost_window_days" in res.attributes:
        basis["window_days"] = res.attributes["cost_window_days"]
    if res.cost_source == "cost_explorer_tag":
        basis["note"] = "Costo repartido entre recursos que comparten la etiqueta de asignación; puede incluir otros servicios"
    return basis


def evaluate_all(resources: list[NormalizedResource], cfg: RuleConfig,
                 skipped: list[Finding] | None = None) -> list[Finding]:
    """Aplica las reglas. Con ``cfg.require_real_cost`` descarta (y devuelve en ``skipped``) las propuestas cuyo costo es
    solo una estimación: no se propone apagar ni reducir algo cuyo gasto real no se ha comprobado."""
    out: list[Finding] = []
    for res in resources:
        for f in evaluate_resource(res, cfg):
            if cfg.require_real_cost and not has_real_cost(res):
                if skipped is not None:
                    skipped.append(f)
                continue
            f.evidence["cost_basis"] = cost_basis(res)
            out.append(f)
    return out
