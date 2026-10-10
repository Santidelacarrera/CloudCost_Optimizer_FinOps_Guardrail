"""Validación del colector de Kubernetes contra un clúster REAL de laboratorio (minikube) con un informe reproducible.

Qué hace
    1. Consulta un Prometheus REAL (kube-state-metrics + cAdvisor) con el mismo `KubernetesCollector` que usa el producto. Solo `GET
       /api/v1/query`: no habla con la API de Kubernetes, no necesita kubeconfig y no modifica nada.
    2. Guarda una INSTANTÁNEA (`snapshot.json`): inventario de workloads, hallazgos calculados, consultas fallidas y comprobaciones.
    3. Genera un informe Markdown DETERMINISTA a partir de la instantánea (misma instantánea → mismo texto, byte a byte; SHA-256 en
       `report.sha256`). Se regenera con `--from-snapshot`, sin acceso al clúster.

Qué NO hace: no escribe en el clúster, no usa la base de datos, no propone ni aplica cambios. No mide facturación real: un clúster local
no tiene factura; el coste es el de lo RESERVADO (requests × réplicas) con precios por vCPU y GiB declarados. Ver docs/k8s-lab-validation.md.
"""
from __future__ import annotations

import platform
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import redaction
from .collectors.cloudhttp import BearerClient, CloudApiError
from .collectors.kubernetes import KubernetesCollector, PrometheusUrlError, validate_prometheus_url
from .domain import risk as risk_mod
from .domain.models import NormalizedResource
from .domain.rules import RuleConfig, evaluate_all
from .lab import _finding_dict, _md, _usd, load_snapshot, snapshot_digest, write_outputs
from .secrets import SecretResolver

SNAPSHOT_VERSION = 1
TOOL = "cloudcost-k8s-lab"
__all__ = ["collect_snapshot", "render_report", "write", "load_snapshot", "snapshot_digest"]
DEFAULT_NAMESPACES = ["lab-dev"]

# Cargas de `infrastructure/k8s-lab/workloads.yaml` y lo que el sistema DEBE hacer con cada una.
LAB_EXPECTATIONS: dict[str, tuple[str, str]] = {
    "overprovisioned-web": ("finding_cpu_and_memory", "reserva 500m / 512Mi y casi no usa nada: debe proponer bajar CPU y memoria"),
    "well-sized": ("no_finding", "reserva lo que usa: no debe proponer nada"),
    "hpa-protected": ("finding_memory_only", "tiene HPA: debe proponer bajar solo la memoria y no tocar la CPU"),
}


class Recorder:
    """Cliente HTTP que cuenta las consultas enviadas (y las fallidas) sin guardar su contenido."""

    def __init__(self, inner):
        self._inner, self.total, self.failed = inner, 0, 0

    def get_json(self, url, params=None):
        self.total += 1
        try:
            return self._inner.get_json(url, params)
        except Exception:
            self.failed += 1
            raise


def _hint_for(exc: Exception) -> str:
    code = getattr(exc, "status", None)
    if isinstance(exc, CloudApiError) and code:
        return f"Prometheus respondió HTTP {code}: comprueba la URL y que el servicio es Prometheus."
    return ("No se pudo conectar con Prometheus. ¿Está abierto el túnel? Ejecuta en otra terminal "
            "`kubectl -n monitoring port-forward svc/prometheus-server 9090:80` y vuelve a intentarlo.")


def check_expectations(resources: list[dict[str, Any]], findings: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Compara lo que el sistema hizo con lo que debía para las cargas conocidas del laboratorio. Vacío si el clúster no las tiene."""
    out: list[dict[str, str]] = []
    present = {r["attributes"].get("workload") for r in resources}
    by_workload: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        by_workload.setdefault(str(f.get("workload") or ""), []).append(f)
    for name, (expected, why) in sorted(LAB_EXPECTATIONS.items()):
        if name not in present:
            continue
        mine = by_workload.get(name, [])
        targets = {k for f in mine for k in (f.get("params_target") or {})}
        if expected == "no_finding":
            ok = not mine
        elif expected == "finding_cpu_and_memory":
            ok = {"cpu", "memory"} <= targets
        else:
            ok = targets == {"memory"}
        out.append({"workload": name, "expected": why, "observed": ", ".join(sorted(targets)) or "sin hallazgos", "result": "OK" if ok else "FALLA"})
    return out


def collect_snapshot(*, cluster_ref: str, prometheus_url: str, namespaces: list[str] | None = None, min_observation_days: int | None = None,
                     cpu_hour: float | None = None, mem_gib_hour: float | None = None, client=None,
                     now: datetime | None = None) -> dict[str, Any]:
    started = now or datetime.now(timezone.utc)
    ns = namespaces or DEFAULT_NAMESPACES
    default_days = RuleConfig().k8s_min_observation_days
    rule_cfg = RuleConfig(k8s_min_observation_days=min_observation_days) if min_observation_days is not None else RuleConfig()
    aborted: str | None = None
    result = None
    rec: Recorder | None = None
    cfg: dict[str, Any] = {"prometheus_url": prometheus_url, "namespaces": ns}
    if cpu_hour:
        cfg["cpu_hour_usd"] = cpu_hour
    if mem_gib_hour:
        cfg["mem_gib_hour_usd"] = mem_gib_hour
    account = {"provider": "kubernetes", "account_ref": cluster_ref, "regions": ["all"], "provider_config": cfg, "organization_id": None}
    try:
        base = validate_prometheus_url(prometheus_url)
        inner = client or BearerClient(lambda: "", {urlparse(base).hostname}, allow_http=True, timeout=120.0)
        rec = Recorder(inner)
        rec.get_json(f"{base}/api/v1/query", {"query": "count(up)", "time": f"{started.timestamp():.0f}"})     # alcanzable y es Prometheus
        collector = KubernetesCollector(account, SecretResolver(), client=rec, now=started)
        result = collector.collect()
    except PrometheusUrlError as exc:
        aborted = f"URL de Prometheus no válida: {exc}"
    except Exception as exc:                                      # noqa: BLE001
        aborted = _hint_for(exc)
    resources = sorted(result.resources, key=lambda r: r.resource_id) if result else []
    findings = []
    for f in evaluate_all(resources, rule_cfg):
        d = _finding_dict(f, risk_mod.classify_risk(f), cluster_ref)
        d["params_target"] = dict(f.params.get("target") or {})
        d["workload"] = f.params.get("workload")
        findings.append(d)
    findings.sort(key=lambda d: (-d["estimated_monthly_savings"], d["resource_id"], d["rule_id"]))
    res_dicts = [_resource_dict(r) for r in resources]
    return {
        "version": SNAPSHOT_VERSION,
        "meta": {"tool": TOOL, "generated_at": started.replace(microsecond=0).isoformat(), "cluster_ref": cluster_ref,
                 "prometheus_host": urlparse(prometheus_url).hostname or "", "namespaces": sorted(ns),
                 "min_observation_days": rule_cfg.k8s_min_observation_days, "default_min_observation_days": default_days,
                 "queries": rec.total if rec else 0, "queries_failed": rec.failed if rec else 0, "aborted": aborted,
                 "python": platform.python_version()},
        "warnings": [redaction.redact_text(w) for w in (result.warnings if result else [])], "partial": bool(result.partial) if result else False,
        "resources": res_dicts, "findings": findings, "checks": check_expectations(res_dicts, findings),
    }


def _resource_dict(r: NormalizedResource) -> dict[str, Any]:
    d = r.to_dict()
    d["attributes"] = {k: v for k, v in d["attributes"].items() if k != "cluster"}
    return d


# --------------------------------------------------------------------------- informe (determinista)
def _age(hours: float | None) -> str:
    if hours is None:
        return "—"
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.1f} d"


def render_report(snap: dict[str, Any]) -> str:
    m, res, findings = snap["meta"], snap["resources"], snap["findings"]
    override = m["min_observation_days"] < m["default_min_observation_days"]
    L: list[str] = []
    a = L.append
    a(f"# Validación del colector de Kubernetes · clúster `{_md(m['cluster_ref'])}`")
    a("")
    a(f"> Generado el {m['generated_at']} con `{m['tool']}` (Python {m['python']}). Huella de la instantánea: `{snapshot_digest(snap)}`.")
    a("")
    a("**Cómo reproducir este informe:** `python scripts/k8s_lab.py --from-snapshot snapshot.json` genera exactamente el mismo texto "
      "(su SHA-256 está en `report.sha256`). No hace falta acceso al clúster.")
    a("")
    a("## 1. Resultado de la validación")
    a("")
    verdict = "**INCOMPLETA**" if m["aborted"] else "**con incidencias**" if snap["partial"] or m["queries_failed"] else "**sin incidencias**"
    a(f"- Prometheus: `{_md(m['prometheus_host'])}`. Namespaces evaluados: {', '.join(m['namespaces'])}.")
    a(f"- Consultas enviadas: {m['queries']} (todas `GET /api/v1/query`, solo lectura); fallidas: {m['queries_failed']}.")
    a(f"- Resultado: {verdict}.")
    if m["aborted"]:
        a(f"- Motivo de la interrupción: {_md(m['aborted'])}")
    if override:
        a(f"- ⚠️ **Umbral de observación reducido a {m['min_observation_days']} días** (el del producto es {m['default_min_observation_days']}). "
          "Solo para laboratorio: con tan pocos datos los hallazgos NO sirven para decidir cambios en producción.")
    a("")
    a("## 2. Inventario de workloads")
    a("")
    a("| Workload | Réplicas | CPU pedida | CPU p95 / máx | Mem pedida | Mem máx | HPA | Antigüedad | Días completos de datos | Coste reservado/mes |")
    a("|---|---:|---:|---|---:|---:|---|---:|---:|---:|")
    for r in res:
        x = r["attributes"]
        p95 = "—" if x.get("cpu_p95_cores") is None else f"{x['cpu_p95_cores'] * 1000:.0f}m / {x['cpu_max_cores'] * 1000:.0f}m"
        mem = "—" if x.get("mem_max_bytes") is None else f"{x['mem_max_bytes'] / 2**20:.0f}Mi"
        a(f"| {_md(r['name'])} | {x['replicas']} | {x['cpu_request'] * 1000:.0f}m | {p95} | {x['mem_request'] / 2**20:.0f}Mi | {mem} | "
          f"{'sí' if x.get('hpa') else 'no'} | {_age(x.get('age_hours'))} | {r['observation_days']} | {_usd(r['monthly_cost'])} |")
    if not res:
        a("| — | — | — | — | — | — | — | — | — | — |")
    a("")
    if not res and not m["aborted"]:
        a("Prometheus no devolvió workloads con requests: comprueba que kube-state-metrics está instalado y que los namespaces son los correctos.")
        a("")
    a("## 3. Hallazgos (ahorro ESTIMADO, no observado)")
    a("")
    total = sum(f["estimated_monthly_savings"] for f in findings)
    a(f"{len(findings)} hallazgos; ahorro estimado de lo reservado **USD {_usd(total)}/mes** (precios por vCPU y GiB declarados; no es una factura).")
    a("")
    a("| Workload | Cambio propuesto | Ahorro/mes | Riesgo | Confianza |")
    a("|---|---|---:|---|---:|")
    for f in findings:
        change = ", ".join(f"{k}: {v}" for k, v in sorted((f.get("params_target") or {}).items())) or "—"
        a(f"| {_md(f.get('workload') or f['resource_id'])} | {_md(change)} | {_usd(f['estimated_monthly_savings'])} | {f['risk']} | {f['confidence']:.0%} |")
    if not findings:
        a("| — | — | — | — | — |")
    a("")
    if findings:
        a("Fórmulas aplicadas: " + ", ".join(sorted({f"`{f['formula_id']}`" for f in findings if f.get("formula_id")})) + ". Detalle: docs/savings-methodology.md.")
        a("")
    checks = snap.get("checks") or []
    a("## 4. Comprobaciones del laboratorio")
    a("")
    if checks:
        a("| Carga | Debía ocurrir | Ocurrió | Resultado |")
        a("|---|---|---|---|")
        for c in checks:
            a(f"| {c['workload']} | {_md(c['expected'])} | {_md(c['observed'])} | {c['result']} |")
        failed = sum(1 for c in checks if c["result"] != "OK")
        a("")
        a(f"**{len(checks) - failed} de {len(checks)} comprobaciones correctas.**")
    else:
        a("El clúster no tiene las cargas de `infrastructure/k8s-lab/workloads.yaml`; no hay comprobaciones automáticas.")
    a("")
    a("## 5. Incidencias")
    a("")
    if snap["warnings"]:
        for w in snap["warnings"]:
            a(f"- {_md(w)}")
    else:
        a("Ninguna.")
    a("")
    a("## 6. Limitaciones de esta validación")
    a("")
    for line in (
        "Valida el colector contra UN clúster local (minikube) con cargas sintéticas; no demuestra que funcione igual en clústeres grandes, con muchos namespaces o con otra distribución de Prometheus.",
        "El coste es el de lo RESERVADO con precios por vCPU/GiB declarados; un clúster local no tiene factura, así que no valida la medición de ahorro contra costes reales.",
        "Con pocas horas de datos las medias y percentiles no son representativos: solo el umbral de producción (7 días) garantiza hallazgos fiables. «Días completos de datos» trunca a días enteros (16 h cuentan como 0).",
        "El ahorro de los hallazgos es una estimación con la fórmula indicada, no un ahorro observado.",
    ):
        a(f"- {line}")
    a("")
    return "\n".join(L)


def write(snap: dict[str, Any], out_dir: str) -> dict[str, str]:
    return write_outputs(snap, out_dir, render=render_report)
