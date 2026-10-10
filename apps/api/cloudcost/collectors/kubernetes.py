"""Conector Kubernetes de SOLO LECTURA: compara lo que cada contenedor RESERVA (requests/limits) con lo que USA de verdad.

No habla con la API de Kubernetes ni necesita un kubeconfig: consulta un Prometheus que ya recoge
  * kube-state-metrics  (requests, limits, propietarios, HPA, OOM, etiquetas de Helm), y
  * cAdvisor/kubelet    (container_cpu_usage_seconds_total, container_memory_working_set_bytes).
Solo se hacen consultas `GET /api/v1/query`. Un workload = (namespace, Deployment|StatefulSet|DaemonSet, contenedor).
Los pods que ya no existen (por despliegues recientes) se siguen contando: el uso se agrega por workload, no por pod.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..domain.k8s import DEFAULT_CPU_CORE_HOUR, DEFAULT_MEM_GIB_HOUR, reserved_monthly_cost
from ..domain.models import NormalizedResource
from ..secrets import SecretResolver
from .base import CollectionResult
from .cloudhttp import BearerClient, CloudApiError, JsonGetter
from .common import finish

log = logging.getLogger(__name__)
WINDOW_DAYS = 14
STEP = "10m"                          # resolución de las subconsultas de CPU (la tasa también usa 10m: no quedan huecos)
QUERY_TIMEOUT = 120.0
SYSTEM_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")
SUPPORTED_OWNERS = ("Deployment", "StatefulSet", "DaemonSet")
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_ENV_TOKENS = (("production", ("prod", "production", "prd", "live")), ("staging", ("stg", "stage", "staging", "preprod", "uat")),
               ("development", ("dev", "development", "sandbox")), ("test", ("test", "testing", "qa")))
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


class PrometheusUrlError(ValueError):
    pass


def validate_prometheus_url(url: str) -> str:
    """https (o http solo dentro del clúster: *.svc, *.svc.cluster.local, localhost); sin credenciales en la URL; nunca metadatos de la nube."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise PrometheusUrlError("La URL de Prometheus debe ser http(s)://host[:puerto][/ruta]")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PrometheusUrlError("La URL de Prometheus no puede llevar credenciales, parámetros ni fragmento")
    if host in _BLOCKED_HOSTS:
        raise PrometheusUrlError("Host no permitido")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved):
        raise PrometheusUrlError("Dirección no permitida (enlace local, metadatos de la nube)")
    internal = host == "localhost" or host.endswith((".svc", ".svc.cluster.local")) or (ip is not None and ip.is_loopback)
    if parsed.scheme == "http" and not internal:
        raise PrometheusUrlError("http solo se admite dentro del clúster (*.svc, *.svc.cluster.local, localhost); usa https")
    return url.rstrip("/")


def environment_for_namespace(namespace: str, default: str = "unknown") -> str:
    parts = set(re.split(r"[^a-z0-9]+", namespace.lower()))
    for env, tokens in _ENV_TOKENS:
        if parts & set(tokens):
            return env
    return default


def _matchers(include: list[str], exclude: list[str]) -> str:
    """Selector de namespaces para PromQL. Los nombres son etiquetas DNS validadas: no pueden inyectar PromQL."""
    for ns in include + exclude:
        if not _DNS_LABEL.match(ns):
            raise ValueError(f"namespace inválido: {ns!r}")
    parts = []
    if include:
        parts.append('namespace=~"' + "|".join(include) + '"')
    if exclude:
        parts.append('namespace!~"' + "|".join(exclude) + '"')
    return ",".join(parts)


class KubernetesCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, client: JsonGetter | None = None,
                 on_api_error=None, now: datetime | None = None):
        self.account = account
        self.secrets = secrets
        self.cluster = account["account_ref"]
        cfg = account.get("provider_config") or {}
        self.cfg = cfg
        self.on_api_error = on_api_error or (lambda api: None)
        self._client = client
        self._base = cfg.get("prometheus_url") or "http://prometheus.invalid"
        self._now = now or datetime.now(timezone.utc)
        self.warnings: list[str] = []
        self.partial = False
        include = list(cfg.get("namespaces") or [])
        exclude = list(cfg.get("exclude_namespaces") or []) or list(SYSTEM_NAMESPACES)
        self._ns = _matchers(include, exclude)
        self.cpu_hour = float(cfg.get("cpu_hour_usd") or DEFAULT_CPU_CORE_HOUR)
        self.mem_hour = float(cfg.get("mem_gib_hour_usd") or DEFAULT_MEM_GIB_HOUR)

    # ------------------------------------------------------------------ infraestructura
    def _http(self) -> JsonGetter:
        if self._client is None:
            try:
                base = validate_prometheus_url(self.cfg.get("prometheus_url", ""))
            except PrometheusUrlError as exc:
                raise PermissionError(str(exc)) from exc               # configuración inválida: no tiene sentido reintentar
            token = self.secrets.resolve(self.cfg.get("credentials_ref"), self.account.get("organization_id")) if self.cfg.get("credentials_ref") else ""
            self._base = base
            self._client = BearerClient(lambda: token or "", {urlparse(base).hostname}, allow_http=True, timeout=QUERY_TIMEOUT)
        return self._client

    def _query(self, promql: str) -> list[dict[str, Any]]:
        body = self._http().get_json(f"{self._base}/api/v1/query", {"query": promql, "time": f"{self._now.timestamp():.0f}"})
        if body.get("status") != "success":
            raise CloudApiError(0, str(body.get("errorType") or "query_failed")[:60])
        return body.get("data", {}).get("result", [])

    def _safe(self, name: str, promql: str) -> list[dict[str, Any]]:
        try:
            return self._query(promql)
        except Exception as exc:                                       # noqa: BLE001
            self.on_api_error(f"prometheus:{name}")
            self.partial = True
            self.warnings.append(f"prometheus:{name}: {getattr(exc, 'code', '') or type(exc).__name__}")
            log.warning("prometheus query failed name=%s err=%s", name, type(exc).__name__)
            return []

    @staticmethod
    def _values(result: list[dict[str, Any]], *keys: str) -> dict[tuple, float]:
        out: dict[tuple, float] = {}
        for row in result:
            try:
                value = float(row["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if value != value:                                         # NaN
                continue
            out[tuple(row.get("metric", {}).get(k, "") for k in keys)] = value
        return out

    # ------------------------------------------------------------------ colección
    def collect(self) -> CollectionResult:
        self._http()                  # configuración o credenciales inválidas: error claro y sin reintentos
        d, ns = WINDOW_DAYS, self._ns
        sel = f'container!="",container!="POD"{"," + ns if ns else ""}'
        ksm = f"{{{ns}}}" if ns else ""
        pc = ("namespace", "pod", "container")
        cpu_rate = f'sum by (namespace,pod,container) (rate(container_cpu_usage_seconds_total{{{sel}}}[{STEP}]))'
        extra = "," + ns if ns else ""

        def resource(metric: str, which: str) -> str:
            return f'max by (namespace,pod,container) ({metric}{{resource="{which}"{extra}}})'

        req_cpu = self._values(self._safe("requests_cpu", resource("kube_pod_container_resource_requests", "cpu")), *pc)
        req_mem = self._values(self._safe("requests_memory", resource("kube_pod_container_resource_requests", "memory")), *pc)
        lim_cpu = self._values(self._safe("limits_cpu", resource("kube_pod_container_resource_limits", "cpu")), *pc)
        lim_mem = self._values(self._safe("limits_memory", resource("kube_pod_container_resource_limits", "memory")), *pc)
        cpu_avg = self._values(self._safe("cpu_avg", f"avg_over_time({cpu_rate}[{d}d:{STEP}])"), *pc)
        cpu_p95 = self._values(self._safe("cpu_p95", f"quantile_over_time(0.95, {cpu_rate}[{d}d:{STEP}])"), *pc)
        cpu_max = self._values(self._safe("cpu_max", f"max_over_time({cpu_rate}[{d}d:{STEP}])"), *pc)
        mem_series = f'max by (namespace,pod,container) (container_memory_working_set_bytes{{{sel}}})'
        mem_avg = self._values(self._safe("mem_avg", f"avg_over_time({mem_series}[{d}d:{STEP}])"), *pc)
        mem_max = self._values(self._safe("mem_max", f"max by (namespace,pod,container) (max_over_time(container_memory_working_set_bytes{{{sel}}}[{d}d]))"), *pc)
        oom = self._values(self._safe("oom", f'max by (namespace,pod,container) (kube_pod_container_status_last_terminated_reason{{reason="OOMKilled"{"," + ns if ns else ""}}})'), *pc)
        owners = self._values(self._safe("pod_owner", f"max by (namespace,pod,owner_kind,owner_name) (max_over_time(kube_pod_owner{ksm}[{d}d]))"),
                              "namespace", "pod", "owner_kind", "owner_name")
        rs_owner = self._values(self._safe("replicaset_owner", f'max by (namespace,replicaset,owner_kind,owner_name) (max_over_time(kube_replicaset_owner{{owner_kind="Deployment"{"," + ns if ns else ""}}}[{d}d]))'),
                                "namespace", "replicaset", "owner_kind", "owner_name")
        hpa = self._values(self._safe("hpa", f"max by (namespace,scaletargetref_kind,scaletargetref_name) (kube_horizontalpodautoscaler_info{ksm})"),
                           "namespace", "scaletargetref_kind", "scaletargetref_name")
        labels = self._label_sets(self._safe("pod_labels", f"max by (namespace,pod,label_app_kubernetes_io_instance,label_app_kubernetes_io_name,label_helm_sh_chart) (kube_pod_labels{ksm})"))
        created: dict[tuple[str, str, str], float] = {}
        for kind, metric in (("Deployment", "kube_deployment_created"), ("StatefulSet", "kube_statefulset_created"),
                             ("DaemonSet", "kube_daemonset_created")):
            rows = self._safe(f"created_{kind.lower()}", f"max by (namespace,{kind.lower()}) ({metric}{ksm})")
            for (ns_, name), ts in self._values(rows, "namespace", kind.lower()).items():
                created[(kind, ns_, name)] = ts
        coverage = self._values(self._safe("coverage", f"time() - min(min_over_time(timestamp(up)[{d}d:1h]))"))
        coverage_days = (coverage.get(()) or 0.0) / 86400

        # ---- pod -> workload
        rs_to_deploy = {(ns_, rs): name for (ns_, rs, kind, name) in rs_owner}
        pod_workload: dict[tuple[str, str], tuple[str, str]] = {}
        for (ns_, pod, kind, name) in owners:
            if kind == "ReplicaSet" and (ns_, name) in rs_to_deploy:
                pod_workload[(ns_, pod)] = ("Deployment", rs_to_deploy[(ns_, name)])
            elif kind in ("StatefulSet", "DaemonSet"):
                pod_workload[(ns_, pod)] = (kind, name)

        # ---- agregación por (workload, contenedor)
        current: dict[tuple, list[tuple[str, str]]] = defaultdict(list)       # pods vivos hoy
        history: dict[tuple, list[tuple[str, str, str]]] = defaultdict(list)  # todos los pods de la ventana
        for key in {*req_cpu, *req_mem}:
            ns_, pod, container = key
            if (ns_, pod) in pod_workload:
                current[(ns_, *pod_workload[(ns_, pod)], container)].append((ns_, pod))
        for key in {*cpu_avg, *cpu_p95, *cpu_max, *mem_max, *mem_avg}:
            ns_, pod, container = key
            if (ns_, pod) in pod_workload:
                history[(ns_, *pod_workload[(ns_, pod)], container)].append(key)

        resources: list[NormalizedResource] = []
        skipped_rollouts = 0
        for wkey, pods in sorted(current.items()):
            ns_, kind, workload, container = wkey
            reqs_cpu = {req_cpu.get((ns_, p, container)) for _, p in pods}
            reqs_mem = {req_mem.get((ns_, p, container)) for _, p in pods}
            if None in reqs_cpu or None in reqs_mem or len(reqs_cpu) > 1 or len(reqs_mem) > 1:
                skipped_rollouts += 1           # sin requests o con requests distintos entre pods (despliegue en curso): no se evalúa
                continue
            rc, rm = reqs_cpu.pop(), reqs_mem.pop()
            hist = history.get(wkey, [])
            replicas = len(pods)
            def over_pods(store: dict[tuple, float], hist=hist) -> list[float]:
                return [store[k] for k in hist if k in store]

            avgs, p95s, maxs = over_pods(cpu_avg), over_pods(cpu_p95), over_pods(cpu_max)
            m_avgs, m_maxs = over_pods(mem_avg), over_pods(mem_max)
            first_pod = (ns_, pods[0][1], container)
            age_days = (self._now.timestamp() - created[(kind, ns_, workload)]) / 86400 if (kind, ns_, workload) in created else None
            observed = int(min(WINDOW_DAYS, coverage_days or WINDOW_DAYS, age_days if age_days is not None else WINDOW_DAYS))
            helm = labels.get(pods[0], {})
            helm_info = {k: v for k, v in (("release", helm.get("label_app_kubernetes_io_instance")), ("name", helm.get("label_app_kubernetes_io_name")),
                                           ("chart", helm.get("label_helm_sh_chart"))) if v}
            attrs: dict[str, Any] = {
                "namespace": ns_, "kind": kind, "workload": workload, "container": container, "replicas": replicas,
                "cpu_request": rc, "mem_request": int(rm),
                "cpu_limit": lim_cpu.get(first_pod), "mem_limit": int(lim_mem[first_pod]) if first_pod in lim_mem else None,
                "cpu_avg_cores": round(sum(avgs) / len(avgs), 4) if avgs else None, "cpu_p95_cores": round(max(p95s), 4) if p95s else None,
                "cpu_max_cores": round(max(maxs), 4) if maxs else None,
                "mem_avg_bytes": int(sum(m_avgs) / len(m_avgs)) if m_avgs else None, "mem_max_bytes": int(max(m_maxs)) if m_maxs else None,
                "oom_killed": any(oom.get((ns_, p, container), 0) for _, p in pods) or any(oom.get(k, 0) for k in hist),
                "hpa": (ns_, kind, workload) in hpa,
                "helm": helm_info, "cpu_hour_usd": self.cpu_hour, "mem_gib_hour_usd": self.mem_hour, "cluster": self.cluster,
                "age_hours": round(age_days * 24, 1) if age_days is not None else None,
            }
            resources.append(NormalizedResource(
                "kubernetes", "kubernetes", "k8s_workload", f"{self.cluster}/{ns_}/{kind}/{workload}/{container}", self.cluster,
                name=f"{ns_}/{workload}/{container}", environment=environment_for_namespace(ns_, self.cfg.get("environment", "unknown")),
                state="running", monthly_cost=reserved_monthly_cost(rc, rm, replicas, self.cpu_hour, self.mem_hour),
                cpu_avg=round(100 * attrs["cpu_avg_cores"] / rc, 1) if attrs["cpu_avg_cores"] is not None and rc else None,
                cpu_max=round(100 * attrs["cpu_max_cores"] / rc, 1) if attrs["cpu_max_cores"] is not None and rc else None,
                memory_avg=round(100 * attrs["mem_avg_bytes"] / rm, 1) if attrs["mem_avg_bytes"] is not None and rm else None,
                age_days=int(age_days) if age_days is not None else None, observation_days=observed, attributes=attrs))
        if skipped_rollouts:
            self.warnings.append(f"{skipped_rollouts} contenedores omitidos (sin requests o con requests distintos entre pods: despliegue en curso)")
        if not current and not self.partial:
            self.warnings.append("Prometheus no devolvió pods con requests: ¿kube-state-metrics está instalado y la URL es la correcta?")
        return finish(CollectionResult(), resources, self.warnings, self.partial)

    @staticmethod
    def _label_sets(result: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
        return {(r["metric"].get("namespace", ""), r["metric"].get("pod", "")): r["metric"] for r in result if "metric" in r}
