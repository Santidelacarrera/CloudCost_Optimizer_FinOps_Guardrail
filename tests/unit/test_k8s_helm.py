"""Kubernetes/Helm: colector sobre un Prometheus simulado, regla de requests sobredimensionados y parche de values.yaml.

El Prometheus es falso (respuestas con la forma de /api/v1/query): estas pruebas NO validan que las consultas PromQL devuelvan lo
esperado en un Prometheus real, solo que el colector las construye con los selectores correctos, interpreta sus resultados,
y que la regla y el parche se comportan. Los tests del parche trabajan sobre archivos YAML reales.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml
from cloudcost.collectors import get_collector
from cloudcost.collectors.cloudhttp import BearerClient, CloudApiError
from cloudcost.collectors.kubernetes import (
    KubernetesCollector,
    PrometheusUrlError,
    _matchers,
    environment_for_namespace,
    validate_prometheus_url,
)
from cloudcost.domain import policy as pol
from cloudcost.domain import risk
from cloudcost.domain.k8s import (
    GIB,
    MIB,
    QuantityError,
    format_cpu,
    format_memory,
    parse_cpu,
    parse_memory,
    reserved_monthly_cost,
)
from cloudcost.domain.rules import ACTION_RIGHTSIZE_WORKLOAD, RuleConfig, evaluate_all
from cloudcost.git.base import is_iac_file
from cloudcost.git.local import LocalDemoProvider
from cloudcost.iac.helm import HelmIndex
from cloudcost.iac.patcher import PatchError, build_patch
from cloudcost.iac.terraform import IacIndex
from cloudcost.schemas import CloudAccountIn

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DAY = 86400


# ----------------------------------------------------------------------------------------------------- cantidades
def test_quantities_roundtrip_and_rounding_goes_up():
    assert parse_cpu("500m") == 0.5 and parse_cpu("2") == 2.0 and parse_cpu(0.25) == 0.25
    assert parse_memory("256Mi") == 256 * MIB and parse_memory("1G") == 10**9 and parse_memory("1.5Gi") == int(1.5 * GIB)
    assert format_cpu(0.1234) == "124m" and format_cpu(2.0) == "2" and format_cpu(0.33) == "330m"
    assert format_memory(256 * MIB + 1) == "257Mi" and format_memory(2 * GIB) == "2Gi" and format_memory(1008 * MIB) == "1008Mi"
    for bad in ("", "abc", "12 Mi", "-1", "1e"):
        with pytest.raises(QuantityError):
            parse_memory(bad)
    assert reserved_monthly_cost(1.0, 2 * GIB, 3) == 87.6                          # (1×0,0316 + 2×0,0042) × 730 × 3


# --------------------------------------------------------------------------------------------- URL y selectores
def test_prometheus_url_rules():
    assert validate_prometheus_url("https://prom.example.com/prometheus/") == "https://prom.example.com/prometheus"
    assert validate_prometheus_url("http://prometheus.monitoring.svc:9090") == "http://prometheus.monitoring.svc:9090"
    assert validate_prometheus_url("http://localhost:9090")
    for bad in ("", "ftp://x", "http://prom.example.com", "https://user:pw@prom.example.com", "https://prom.example.com/?q=1",
                "https://169.254.169.254/latest", "http://metadata.google.internal", "https://0.0.0.0", "javascript:alert(1)"):
        with pytest.raises(PrometheusUrlError):
            validate_prometheus_url(bad)


def test_namespace_matchers_cannot_inject_promql():
    assert _matchers([], ["kube-system", "kube-public"]) == 'namespace!~"kube-system|kube-public"'
    assert _matchers(["shop"], []) == 'namespace=~"shop"'
    for bad in ('x"} or vector(1) #', "UPPER", "a|b", ""):
        with pytest.raises(ValueError):
            _matchers([bad], [])
    assert environment_for_namespace("shop-prod") == "production" and environment_for_namespace("team_qa") == "test"
    assert environment_for_namespace("payments") == "unknown" and environment_for_namespace("payments", "staging") == "staging"


# ---------------------------------------------------------------------------------------------- Prometheus falso
def row(value, **labels):
    return {"metric": labels, "value": [NOW.timestamp(), str(value)]}


def pod_row(value, ns, pod, container):
    return row(value, namespace=ns, pod=pod, container=container)


def build_world():
    """Cuatro workloads + un despliegue en curso. Devuelve {id de consulta: filas}."""
    w: dict[str, list] = {k: [] for k in (
        "req_cpu", "req_mem", "lim_cpu", "lim_mem", "cpu_avg", "cpu_p95", "cpu_max", "mem_avg", "mem_max", "oom", "owner", "rs", "hpa",
        "labels", "created_deployment", "created_statefulset", "created_daemonset", "coverage")}

    def add(ns, pod, container, *, cpu_req, mem_req, cpu_lim=None, mem_lim=None, avg=None, p95=None, mx=None, mavg=None, mmax=None,
            current=True, owner=("ReplicaSet", "")):
        if current:
            w["req_cpu"].append(pod_row(cpu_req, ns, pod, container))
            w["req_mem"].append(pod_row(mem_req, ns, pod, container))
            if cpu_lim:
                w["lim_cpu"].append(pod_row(cpu_lim, ns, pod, container))
            if mem_lim:
                w["lim_mem"].append(pod_row(mem_lim, ns, pod, container))
        for key, v in (("cpu_avg", avg), ("cpu_p95", p95), ("cpu_max", mx), ("mem_avg", mavg), ("mem_max", mmax)):
            if v is not None:
                w[key].append(pod_row(v, ns, pod, container))
        w["owner"].append(row(1, namespace=ns, pod=pod, owner_kind=owner[0], owner_name=owner[1]))

    # checkout: 3 réplicas actuales + 1 pod ya borrado (su historial cuenta), requests == límites (QoS Guaranteed), Helm
    for pod, avg, p95, mx, mmax in (("co-1", .10, .22, .38, 650 * MIB), ("co-2", .12, .25, .40, 700 * MIB), ("co-3", .14, .24, .36, 600 * MIB),
                                    ("co-0", .11, .20, .30, 800 * MIB)):
        add("shop-prod", pod, "app", cpu_req=1, mem_req=2 * GIB, cpu_lim=1, mem_lim=2 * GIB, avg=avg, p95=p95, mx=mx, mavg=400 * MIB,
            mmax=mmax, current=pod != "co-0", owner=("ReplicaSet", "checkout-7d9"))
    w["rs"].append(row(1, namespace="shop-prod", replicaset="checkout-7d9", owner_kind="Deployment", owner_name="checkout"))
    w["labels"] += [row(1, namespace="shop-prod", pod=p, label_app_kubernetes_io_instance="checkout",
                        label_app_kubernetes_io_name="checkout", label_helm_sh_chart="checkout-1.4.2") for p in ("co-1", "co-2", "co-3")]
    w["created_deployment"].append(row(NOW.timestamp() - 30 * DAY, namespace="shop-prod", deployment="checkout"))

    # worker: con HPA -> la CPU no se toca; la memoria sí
    for pod in ("wk-1", "wk-2"):
        add("shop-prod", pod, "w", cpu_req=.5, mem_req=2 * GIB, avg=.02, p95=.05, mx=.10, mavg=150 * MIB, mmax=200 * MIB, owner=("ReplicaSet", "worker-5c"))
    w["rs"].append(row(1, namespace="shop-prod", replicaset="worker-5c", owner_kind="Deployment", owner_name="worker"))
    w["hpa"].append(row(1, namespace="shop-prod", scaletargetref_kind="Deployment", scaletargetref_name="worker"))
    w["created_deployment"].append(row(NOW.timestamp() - 20 * DAY, namespace="shop-prod", deployment="worker"))

    # db (StatefulSet): murió por OOM -> la memoria no se toca; la CPU sí
    add("shop-dev", "db-0", "pg", cpu_req=2, mem_req=4 * GIB, avg=.1, p95=.2, mx=.5, mavg=800 * MIB, mmax=GIB, owner=("StatefulSet", "db"))
    w["oom"].append(pod_row(1, "shop-dev", "db-0", "pg"))
    w["created_statefulset"].append(row(NOW.timestamp() - 10 * DAY, namespace="shop-dev", statefulset="db"))

    # api: bien dimensionado -> sin hallazgo
    add("shop-prod", "api-1", "api", cpu_req=.5, mem_req=512 * MIB, avg=.30, p95=.40, mx=.50, mavg=380 * MIB, mmax=450 * MIB, owner=("ReplicaSet", "api-6f"))
    w["rs"].append(row(1, namespace="shop-prod", replicaset="api-6f", owner_kind="Deployment", owner_name="api"))
    w["created_deployment"].append(row(NOW.timestamp() - 40 * DAY, namespace="shop-prod", deployment="api"))

    # legacy: despliegue en curso (requests distintos entre pods) -> se omite
    add("shop-prod", "lg-a", "main", cpu_req=1, mem_req=GIB, avg=.1, p95=.1, mx=.1, mavg=GIB // 4, mmax=GIB // 4, owner=("ReplicaSet", "legacy-1"))
    add("shop-prod", "lg-b", "main", cpu_req=2, mem_req=GIB, avg=.1, p95=.1, mx=.1, mavg=GIB // 4, mmax=GIB // 4, owner=("ReplicaSet", "legacy-2"))
    w["rs"] += [row(1, namespace="shop-prod", replicaset="legacy-1", owner_kind="Deployment", owner_name="legacy"),
                row(1, namespace="shop-prod", replicaset="legacy-2", owner_kind="Deployment", owner_name="legacy")]
    w["created_deployment"].append(row(NOW.timestamp() - 40 * DAY, namespace="shop-prod", deployment="legacy"))

    w["coverage"].append({"metric": {}, "value": [NOW.timestamp(), str(15 * DAY)]})
    return w


def route(q: str) -> str:
    table = [("kube_pod_container_resource_requests", 'resource="cpu"', "req_cpu"), ("kube_pod_container_resource_requests", 'resource="memory"', "req_mem"),
             ("kube_pod_container_resource_limits", 'resource="cpu"', "lim_cpu"), ("kube_pod_container_resource_limits", 'resource="memory"', "lim_mem"),
             ("quantile_over_time", "", "cpu_p95"), ("avg_over_time(sum by", "", "cpu_avg"), ("max_over_time(sum by", "", "cpu_max"),
             ("avg_over_time(max by", "", "mem_avg"), ("max_over_time(container_memory_working_set_bytes", "", "mem_max"),
             ("OOMKilled", "", "oom"), ("kube_pod_owner", "", "owner"), ("kube_replicaset_owner", "", "rs"),
             ("kube_horizontalpodautoscaler_info", "", "hpa"), ("kube_pod_labels", "", "labels"),
             ("kube_deployment_created", "", "created_deployment"), ("kube_statefulset_created", "", "created_statefulset"),
             ("kube_daemonset_created", "", "created_daemonset"), ("timestamp(up)", "", "coverage")]
    for a, b, key in table:
        if a in q and b in q:
            return key
    raise AssertionError(f"consulta no prevista: {q}")


class FakeProm:
    def __init__(self, world=None, fail=()):
        self.world, self.fail, self.queries = world or build_world(), set(fail), []

    def get_json(self, url, params=None):
        assert url.endswith("/api/v1/query") and set(params) == {"query", "time"}
        self.queries.append(params["query"])
        key = route(params["query"])
        if key in self.fail:
            raise CloudApiError(503, "unavailable")
        return {"status": "success", "data": {"resultType": "vector", "result": self.world[key]}}


class NoSecrets:
    def resolve(self, ref):
        return {"env:PROM_TOKEN": "tok"}.get(ref)


def k8s_account(**cfg):
    return {"provider": "kubernetes", "account_ref": "prod-eks", "regions": ["all"],
            "provider_config": {"prometheus_url": "http://prometheus.monitoring.svc:9090", **cfg}}


@pytest.fixture(scope="module")
def collected():
    prom = FakeProm()
    result = KubernetesCollector(k8s_account(), NoSecrets(), client=prom, now=NOW).collect()
    return result, prom, {r.name: r for r in result.resources}


def test_collector_aggregates_by_workload_including_gone_pods(collected):
    result, _, by = collected
    co = by["shop-prod/checkout/app"]
    a = co.attributes
    assert (co.provider, co.resource_type, co.service, co.state, co.environment) == ("kubernetes", "kubernetes", "k8s_workload", "running", "production")
    assert co.resource_id == "prod-eks/shop-prod/Deployment/checkout/app" and a["replicas"] == 3          # co-0 ya no existe
    assert a["cpu_request"] == 1 and a["mem_request"] == 2 * GIB and a["cpu_limit"] == 1 and a["mem_limit"] == 2 * GIB
    assert a["cpu_p95_cores"] == .25 and a["cpu_max_cores"] == .40                                    # el máximo/p95 incluyen al pod borrado
    assert a["mem_max_bytes"] == 800 * MIB and a["helm"] == {"release": "checkout", "name": "checkout", "chart": "checkout-1.4.2"}
    assert co.observation_days == 14 and a["hpa"] is False and a["oom_killed"] is False
    assert co.monthly_cost == 87.6
    assert by["shop-prod/worker/w"].attributes["hpa"] is True and by["shop-dev/db/pg"].attributes["oom_killed"] is True
    assert by["shop-dev/db/pg"].attributes["kind"] == "StatefulSet" and by["shop-dev/db/pg"].environment == "development"
    assert by["shop-dev/db/pg"].observation_days == 10                                                # edad del StatefulSet < ventana


def test_collector_skips_rollouts_and_reports_it(collected):
    result, _, by = collected
    assert "shop-prod/legacy/main" not in by
    assert any("despliegue en curso" in w for w in result.warnings) and not result.partial


def test_collector_queries_are_get_only_and_exclude_system_namespaces(collected):
    _, prom, _ = collected
    assert prom.queries and all("kube-system|kube-public|kube-node-lease" in q for q in prom.queries if "timestamp(up)" not in q)
    assert all(q.count("(") == q.count(")") and q.count("{") == q.count("}") for q in prom.queries)


def test_rule_proposes_rightsizing_with_safe_exceptions(collected):
    findings = {f.resource.name: f for f in evaluate_all(collected[0].resources, RuleConfig())}
    assert set(findings) == {"shop-prod/checkout/app", "shop-prod/worker/w", "shop-dev/db/pg"}           # api no, legacy no
    co = findings["shop-prod/checkout/app"]
    assert co.action == ACTION_RIGHTSIZE_WORKLOAD and not co.destructive
    assert co.params["current"] == {"cpu": "1", "memory": "2Gi"}
    assert co.params["target"] == {"cpu": "330m", "memory": "1008Mi"}                    # p95×1,3 ; máx(800 Mi)×1,25 redondeado a 16 Mi
    assert co.params["limits"] == {"cpu": "330m", "memory": "1008Mi"}                    # límite == request (Guaranteed): se mantiene igual
    expected = reserved_monthly_cost(1 - .33, 2 * GIB - 1008 * MIB, 3)
    assert co.estimated_monthly_savings == expected and 50 < expected < 60 and co.projected_monthly_cost == round(87.6 - expected, 2)
    assert .7 < co.confidence < .9 and co.evidence["cpu_p95_cores"] == .25 and co.evidence["price_basis"]["cpu_core_hour_usd"] == .0316
    wk, db = findings["shop-prod/worker/w"], findings["shop-dev/db/pg"]
    assert set(wk.params["target"]) == {"memory"} and "HPA" in wk.summary                  # con HPA la CPU no se toca
    assert wk.params["limits"] == {}                                                        # sin límites declarados: no se inventan
    assert set(db.params["target"]) == {"cpu"} and "OOM" in db.summary                      # tras un OOM la memoria no se toca


def test_rule_is_conservative_on_thresholds():
    prom = FakeProm()
    for r in prom.world["cpu_p95"] + prom.world["cpu_max"]:
        if r["metric"]["pod"].startswith("co-"):
            r["value"][1] = "0.9"                                                               # uso pico ≈ lo pedido: nada que recortar en CPU
    res = KubernetesCollector(k8s_account(), NoSecrets(), client=prom, now=NOW).collect()
    findings = {f.resource.name: f for f in evaluate_all(res.resources, RuleConfig())}
    assert "cpu" not in findings["shop-prod/checkout/app"].params["target"] and "memory" in findings["shop-prod/checkout/app"].params["target"]
    assert "cpu" in findings["shop-dev/db/pg"].params["target"]                                 # el resto no se ve afectado
    young = KubernetesCollector(k8s_account(), NoSecrets(), client=FakeProm(), now=NOW).collect()
    for r in young.resources:
        r.observation_days = 3
    assert evaluate_all(young.resources, RuleConfig()) == []                                 # menos de 7 días de datos


def test_risk_policy_and_stable_identity(collected):
    co = {f.resource.name: f for f in evaluate_all(collected[0].resources, RuleConfig())}["shop-prod/checkout/app"]
    assert risk.classify_risk(co) == "MEDIUM"                                                # producción: no es LOW
    decision = pol.evaluate(action=co.action, risk="MEDIUM", environment=co.resource.environment, confidence=co.confidence, cfg=pol.PolicyConfig())
    assert decision.allowed and decision.approvals_required == 1 and not decision.automation_blocked
    shifted = evaluate_all(KubernetesCollector(k8s_account(), NoSecrets(), client=_with(mem_max=500), now=NOW).collect().resources, RuleConfig())
    co2 = {f.resource.name: f for f in shifted}["shop-prod/checkout/app"]
    assert co2.params["target"] != co.params["target"] and co2.dedupe_key("acct") == co.dedupe_key("acct")   # misma recomendación, valores nuevos


def _with(mem_max):
    prom = FakeProm()
    for r in prom.world["mem_max"]:
        if r["metric"]["namespace"] == "shop-prod" and r["metric"]["pod"].startswith("co-"):
            r["value"][1] = str(mem_max * MIB)
    return prom


def test_partial_failures_do_not_abort():
    errors = []
    res = KubernetesCollector(k8s_account(), NoSecrets(), client=FakeProm(fail=["hpa", "labels"]), on_api_error=errors.append, now=NOW).collect()
    assert res.partial and "prometheus:hpa" in errors and any("prometheus:pod_labels" in w for w in res.warnings)
    assert res.resources and not any(r.attributes["hpa"] for r in res.resources)           # sin el dato, sin HPA registrada
    assert next(r for r in res.resources if r.name.endswith("checkout/app")).attributes["helm"] == {}


def test_invalid_configuration_fails_fast_and_without_retries():
    for cfg in ({"prometheus_url": "http://prometheus.example.com"}, {"prometheus_url": "https://169.254.169.254"}, {"prometheus_url": ""}):
        with pytest.raises(PermissionError):
            KubernetesCollector(k8s_account(**cfg), NoSecrets(), now=NOW).collect()
    assert isinstance(get_collector(k8s_account(), secrets=NoSecrets(), demo_enabled=False), KubernetesCollector)


class Resp:
    status_code, headers = 200, {}

    def json(self):
        return {"status": "success", "data": {"result": []}}


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return Resp()


def test_http_client_sends_no_auth_header_without_token_and_only_to_its_host():
    s = Session()
    client = BearerClient(lambda: "", {"prometheus.monitoring.svc"}, session=s, allow_http=True)
    client.get_json("http://prometheus.monitoring.svc:9090/api/v1/query", {"query": "up"})
    assert "Authorization" not in s.calls[0][1]["headers"]
    with pytest.raises(CloudApiError):
        client.get_json("http://evil.example/api/v1/query")
    BearerClient(lambda: "tok", {"p.example.com"}, session=s).get_json("https://p.example.com/x")
    assert s.calls[1][1]["headers"]["Authorization"] == "Bearer tok"
    with pytest.raises(CloudApiError):                                                       # sin allow_http, http se rechaza
        BearerClient(lambda: "", {"p.example.com"}, session=s).get_json("http://p.example.com/x")


def test_account_validation_for_kubernetes():
    ok = dict(provider="kubernetes", account_ref="prod-eks", display_name="Prod EKS", prometheus_url="https://prom.example.com/")
    acct = CloudAccountIn(**ok, credentials_ref="env:PROM_TOKEN", exclude_namespaces=["kube-system", "monitoring"], cpu_hour_usd=.04)
    assert acct.regions == ["all"] and acct.provider_config == {
        "prometheus_url": "https://prom.example.com", "credentials_ref": "env:PROM_TOKEN",
        "exclude_namespaces": ["kube-system", "monitoring"], "cpu_hour_usd": .04}
    for patch in ({"prometheus_url": None}, {"prometheus_url": "http://prom.example.com"}, {"namespaces": ["Bad NS"]},
                  {"credentials_ref": "token-en-claro"}, {"cpu_hour_usd": -1}):
        with pytest.raises(ValueError):
            CloudAccountIn(**{**ok, **patch})


# ------------------------------------------------------------------------------------------------ values.yaml
VALUES = '''# Chart de checkout — el equipo de pagos es dueño de este archivo (ñandú)
replicaCount: 3
image:
  repository: ghcr.io/acme/checkout   # fijado por CI
resources:
  requests:
    cpu: "1"          # un core entero
    memory: 2Gi
  limits:
    cpu: "1"
    memory: 2Gi
sidecar:
  resources:
    requests: {cpu: 50m, memory: 64Mi}
    limits: {cpu: 100m, memory: 128Mi}
'''
CHART = "apiVersion: v2\nname: checkout\nversion: 1.4.2\n"


def checkout_resource(collected):
    return next(r for r in collected[0].resources if r.name == "shop-prod/checkout/app")


def checkout_finding(collected):
    return next(f for f in evaluate_all(collected[0].resources, RuleConfig()) if f.resource.name == "shop-prod/checkout/app")


def test_patch_edits_only_the_numbers_and_keeps_comments_and_quotes(collected):
    files = {"charts/checkout/values.yaml": VALUES, "charts/checkout/Chart.yaml": CHART}
    index = IacIndex.build(files)
    res, f = checkout_resource(collected), checkout_finding(collected)
    block = index.match(res)
    assert block and block.address == "charts/checkout/values.yaml#resources"                # el sidecar (50m/64Mi) NO coincide
    patch = build_patch(action=f.action, params=f.params, block=block, index=index)
    assert patch.path == "charts/checkout/values.yaml"
    expected = (VALUES.replace('cpu: "1"          # un core entero', 'cpu: "330m"          # un core entero')
                .replace("    memory: 2Gi\n  limits:", "    memory: 1008Mi\n  limits:")
                .replace('  limits:\n    cpu: "1"\n    memory: 2Gi', '  limits:\n    cpu: "330m"\n    memory: 1008Mi'))
    assert patch.new_text == expected
    assert "# un core entero" in patch.new_text and "(ñandú)" in patch.new_text and "{cpu: 50m, memory: 64Mi}" in patch.new_text
    before, after = yaml.safe_load(VALUES), yaml.safe_load(patch.new_text)
    assert after["resources"] == {"requests": {"cpu": "330m", "memory": "1008Mi"}, "limits": {"cpu": "330m", "memory": "1008Mi"}}
    assert after["sidecar"] == before["sidecar"] and after["replicaCount"] == 3
    assert {v["check"] for v in patch.validations} >= {"yaml_syntax", "only_expected_keys_changed", "reductions_only", "matches_cluster"}
    assert "-    memory: 2Gi" in patch.diff and "+    memory: 1008Mi" in patch.diff


def test_patch_flow_style_crlf_and_multidocument():
    text = "---\r\nname: other\r\n---\r\nresources:\r\n  requests: {cpu: 500m, memory: 1Gi}   # flujo\r\n  limits: {cpu: 1, memory: 2Gi}\r\n"
    index = IacIndex.build({"values-worker.yaml": text})
    res = _res(cpu=.5, mem=GIB, workload="worker")
    block = index.match(res)
    assert block and block.address == "values-worker.yaml#resources"
    params = {"current": {"cpu": "500m", "memory": "1Gi"}, "target": {"memory": "256Mi"}, "limits": {}}
    patch = build_patch(action=ACTION_RIGHTSIZE_WORKLOAD, params=params, block=block, index=index)
    assert patch.new_text == text.replace("memory: 1Gi}   # flujo", "memory: 256Mi}   # flujo")


def _res(*, cpu, mem, workload="checkout", container="app", helm=None):
    from cloudcost.domain.models import NormalizedResource

    return NormalizedResource("kubernetes", "kubernetes", "k8s_workload", f"c/ns/Deployment/{workload}/{container}", "c", name=workload,
                              state="running", attributes={"cpu_request": cpu, "mem_request": mem, "workload": workload,
                                                           "container": container, "helm": helm or {}})


def test_matching_requires_identical_requests_and_explains_why_not():
    idx = IacIndex.build({"values.yaml": VALUES})
    drifted = _res(cpu=2, mem=2 * GIB)                                                          # el clúster pide 2 cores; Git dice 1
    assert idx.match(drifted) is None and idx.why_no_match(drifted)["code"] == "helm_no_match"
    assert idx.match(_res(cpu=.05, mem=64 * MIB, workload="checkout-sidecar")).address == "values.yaml#sidecar.resources"
    assert idx.why_no_match(_res(cpu=1, mem=GIB))["code"] == "helm_no_match"
    assert IacIndex.build({"main.tf": ""}).match(drifted) is None


def test_ambiguity_is_resolved_by_chart_release_or_container_never_guessed():
    two = {"envs/staging/values.yaml": VALUES, "envs/prod/values.yaml": VALUES}
    idx = IacIndex.build(two)
    res = _res(cpu=1, mem=2 * GIB)
    assert idx.match(res) is None and idx.why_no_match(res)["code"] == "helm_ambiguous"
    assert "envs/prod/values.yaml#resources" in idx.why_no_match(res)["message"]
    by_release = _res(cpu=1, mem=2 * GIB, helm={"release": "prod"})
    assert idx.match(by_release).path == "envs/prod/values.yaml"
    sibling = {"charts/a/values.yaml": VALUES, "charts/a/Chart.yaml": "name: a\n", "charts/b/values.yaml": VALUES, "charts/b/Chart.yaml": "name: b\n"}
    assert IacIndex.build(sibling).match(_res(cpu=1, mem=2 * GIB, helm={"chart": "b-0.3.1"})).path == "charts/b/values.yaml"
    multi = "api:\n  resources:\n    requests: {cpu: 1, memory: 2Gi}\nworker:\n  resources:\n    requests: {cpu: 1, memory: 2Gi}\n"
    assert IacIndex.build({"values.yaml": multi}).match(_res(cpu=1, mem=2 * GIB)) is None
    assert IacIndex.build({"values.yaml": multi}).match(_res(cpu=1, mem=2 * GIB, workload="worker", container="worker")).address == "values.yaml#worker.resources"


def test_unsafe_yaml_is_refused():
    anchors = "base: &r\n  requests: {cpu: 1, memory: 2Gi}\nresources: *r\nother:\n  resources: *r\n"
    idx = IacIndex.build({"values.yaml": anchors})
    res = _res(cpu=1, mem=2 * GIB)
    assert idx.match(res) is None and idx.why_no_match(res)["code"] == "helm_alias"
    merged = "resources:\n  <<: {requests: {cpu: 1, memory: 2Gi}}\n  requests: {cpu: 1, memory: 2Gi}\n"
    assert IacIndex.build({"values.yaml": merged}).match(res) is None
    bomb = "a: &a [x, x]\n" + "".join(f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}]\n" for i in range(24))
    assert "values.yaml" in IacIndex.build({"values.yaml": bomb}).parse_errors                 # bomba de alias: error, no cuelgue
    assert "values.yaml" in IacIndex.build({"values.yaml": "a: [unclosed"}).parse_errors


@pytest.mark.parametrize("patch_params, code", [
    ({"target": {"cpu": "2"}}, "not_a_reduction"),
    ({"target": {"cpu": "1"}}, "not_a_reduction"),
    ({"target": {"cpu": "abc"}}, "invalid_params"),
    ({"target": {"gpu": "1"}}, "invalid_params"),
    ({"current": {"cpu": "750m", "memory": "2Gi"}, "target": {"cpu": "300m"}}, "drift"),
    ({"target": {"memory": "1Gi"}, "limits": {"memory": "512Mi"}}, "invalid_params"),
])
def test_patch_refuses_unsafe_params(collected, patch_params, code):
    index = IacIndex.build({"values.yaml": VALUES})
    block = index.match(checkout_resource(collected))
    params = {"current": {"cpu": "1", "memory": "2Gi"}, "limits": {}, **patch_params}
    with pytest.raises(PatchError) as err:
        build_patch(action=ACTION_RIGHTSIZE_WORKLOAD, params=params, block=block, index=index)
    assert err.value.code == code


def test_patch_refuses_limits_that_differ_in_git():
    text = VALUES.replace("  limits:\n    cpu: \"1\"\n    memory: 2Gi", "  limits:\n    cpu: \"2\"\n    memory: 4Gi")
    index = IacIndex.build({"values.yaml": text})
    block = index.match(_res(cpu=1, mem=2 * GIB))
    params = {"current": {"cpu": "1", "memory": "2Gi"}, "target": {"cpu": "300m"}, "limits": {"cpu": "300m"}}
    with pytest.raises(PatchError) as err:
        build_patch(action=ACTION_RIGHTSIZE_WORKLOAD, params=params, block=block, index=index)
    assert err.value.code == "limits_mismatch"


def test_actions_do_not_cross_between_terraform_and_helm(collected):
    idx = IacIndex.build({"values.yaml": VALUES, "main.tf": 'resource "aws_instance" "a" {\n  instance_type = "m5.large"\n}\n'})
    helm_block, tf_block = idx.match(checkout_resource(collected)), idx.blocks[0]
    with pytest.raises(PatchError):
        build_patch(action="RESIZE_INSTANCE", params={}, block=helm_block, index=idx)
    with pytest.raises(PatchError):
        build_patch(action=ACTION_RIGHTSIZE_WORKLOAD, params={}, block=tf_block, index=idx)
    assert idx.block_by_address("values.yaml#resources") is helm_block and idx.block_by_address("aws_instance.a") is tf_block


def test_helm_index_only_reads_values_and_chart_files():
    idx = HelmIndex.build({"k8s/deployment.yaml": "kind: Deployment\n", "x/values.yaml": VALUES, "x/values-prod.yml": VALUES, "Chart.yaml": CHART})
    assert set(idx.files) == {"x/values.yaml", "x/values-prod.yml", "Chart.yaml"} and idx.charts == {"checkout": ""}
    assert [is_iac_file(p) for p in ("a.tf", "d/values.yaml", "values-prod.yml", "Chart.yaml", "d/deployment.yaml", "values.yaml.bak")] == \
        [True, True, True, True, False, False]


def test_local_provider_lists_values_files(tmp_path):
    (tmp_path / "chart").mkdir()
    (tmp_path / "chart/values.yaml").write_text(VALUES)
    (tmp_path / "chart/templates.yaml").write_text("x: 1\n")
    (tmp_path / "main.tf").write_text("")
    files = LocalDemoProvider(str(tmp_path), str(tmp_path / "out")).list_files("r", "main", ["."])
    assert set(files) == {"chart/values.yaml", "main.tf"}
