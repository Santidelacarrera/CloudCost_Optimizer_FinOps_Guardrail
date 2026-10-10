"""k8s-lab: el flujo de validación contra minikube, con un Prometheus simulado que reproduce las tres cargas de infrastructure/k8s-lab/workloads.yaml.

Estas pruebas NO validan contra un clúster real (eso lo hace quien ejecuta el laboratorio); validan que la herramienta recoge, evalúa,
comprueba las expectativas, genera un informe determinista y falla de forma clara cuando Prometheus no está.
"""
from __future__ import annotations

import hashlib
import json

import yaml
from cloudcost import cli, k8s_lab
from cloudcost.collectors.kubernetes import SYSTEM_NAMESPACES
from test_k8s_helm import DAY, MIB, NOW, FakeProm, pod_row, row  # noqa: I001


def lab_world():
    w: dict[str, list] = {k: [] for k in (
        "req_cpu", "req_mem", "lim_cpu", "lim_mem", "cpu_avg", "cpu_p95", "cpu_max", "mem_avg", "mem_max", "oom", "owner", "rs", "hpa",
        "labels", "created_deployment", "created_statefulset", "created_daemonset", "coverage")}

    def deploy(name, replicas, cpu_req, mem_req, avg, p95, mx, mem_max):
        w["rs"].append(row(1, namespace="lab-dev", replicaset=f"{name}-abc", owner_kind="Deployment", owner_name=name))
        w["created_deployment"].append(row(NOW.timestamp() - 3 * DAY, namespace="lab-dev", deployment=name))
        for i in range(replicas):
            pod = f"{name}-abc-{i}"
            w["owner"].append(row(1, namespace="lab-dev", pod=pod, owner_kind="ReplicaSet", owner_name=f"{name}-abc"))
            w["req_cpu"].append(pod_row(cpu_req, "lab-dev", pod, "app"))
            w["req_mem"].append(pod_row(mem_req, "lab-dev", pod, "app"))
            for key, v in (("cpu_avg", avg), ("cpu_p95", p95), ("cpu_max", mx), ("mem_avg", mem_max), ("mem_max", mem_max)):
                w[key].append(pod_row(v, "lab-dev", pod, "app"))

    deploy("overprovisioned-web", 2, 0.5, 512 * MIB, 0.001, 0.002, 0.01, 6 * MIB)
    deploy("well-sized", 1, 0.1, 64 * MIB, 0.095, 0.1, 0.1, 40 * MIB)
    deploy("hpa-protected", 3, 0.2, 768 * MIB, 0.15, 0.19, 0.2, 8 * MIB)
    w["hpa"].append(row(1, namespace="lab-dev", scaletargetref_kind="Deployment", scaletargetref_name="hpa-protected"))
    w["coverage"].append({"metric": {}, "value": [NOW.timestamp(), str(3 * DAY)]})
    return w


class LabProm(FakeProm):
    def __init__(self, world=None, fail=(), down=False):
        super().__init__(world or lab_world(), fail)
        self.down = down

    def get_json(self, url, params=None):
        if self.down:
            raise ConnectionError("refused")
        if params["query"] == "count(up)":
            return {"status": "success", "data": {"resultType": "vector", "result": [row(5)]}}
        return super().get_json(url, params)


_REAL_COLLECT = k8s_lab.collect_snapshot


def snapshot(**kw):
    kw.setdefault("min_observation_days", 0)
    return _REAL_COLLECT(cluster_ref="minikube", prometheus_url="http://localhost:9090", client=kw.pop("client", None) or LabProm(),
                                    now=NOW, **kw)


def test_lab_flags_overprovisioned_ignores_wellsized_and_spares_cpu_under_hpa():
    snap = snapshot()
    by = {f["workload"]: f for f in snap["findings"]}
    assert set(by) == {"overprovisioned-web", "hpa-protected"}                        # well-sized: sin hallazgo
    assert set(by["overprovisioned-web"]["params_target"]) == {"cpu", "memory"}
    assert set(by["hpa-protected"]["params_target"]) == {"memory"}                    # el HPA fija el % de CPU: no se toca
    assert [c["result"] for c in snap["checks"]] == ["OK", "OK", "OK"] and {c["workload"] for c in snap["checks"]} == set(k8s_lab.LAB_EXPECTATIONS)
    assert snap["meta"]["queries_failed"] == 0 and snap["meta"]["aborted"] is None
    assert all(f["estimated_monthly_savings"] > 0 and f["formula_id"] for f in snap["findings"])


def test_a_wrong_expectation_is_reported_as_failure():
    w = lab_world()
    for key in ("req_cpu", "req_mem"):                                                 # well-sized pasa a pedir 4× lo que usa
        for r in w[key]:
            if "well-sized" in r["metric"]["pod"]:
                r["value"][1] = str(float(r["value"][1]) * 4)
    snap = snapshot(client=LabProm(w))
    assert {c["workload"]: c["result"] for c in snap["checks"]}["well-sized"] == "FALLA"


def test_default_threshold_hides_findings_with_few_days_and_report_warns_when_lowered():
    strict = snapshot(min_observation_days=7)
    assert strict["findings"] == [] and strict["meta"]["min_observation_days"] == 7
    low = snapshot()
    report = k8s_lab.render_report(low)
    assert "Umbral de observación reducido a 0 días" in report and "3 de 3 comprobaciones correctas" in report
    assert "Umbral de observación reducido" not in k8s_lab.render_report(strict)


def test_report_is_deterministic_and_regenerable_from_snapshot(tmp_path):
    snap = snapshot()
    assert k8s_lab.render_report(snap) == k8s_lab.render_report(json.loads(json.dumps(snap)))
    paths = k8s_lab.write(snap, str(tmp_path / "a"))
    again = k8s_lab.write(k8s_lab.load_snapshot(paths["snapshot"]), str(tmp_path / "b"))
    assert open(paths["report"], encoding="utf-8").read() == open(again["report"], encoding="utf-8").read()
    digest = hashlib.sha256(open(paths["report"], "rb").read()).hexdigest()
    assert open(paths["digest"]).read().startswith(digest)


def test_prometheus_down_gives_clear_abort_not_a_traceback():
    snap = snapshot(client=LabProm(down=True))
    assert snap["resources"] == [] and "port-forward" in snap["meta"]["aborted"]
    assert "INCOMPLETA" in k8s_lab.render_report(snap)


def test_bad_prometheus_url_is_rejected_before_any_request():
    snap = k8s_lab.collect_snapshot(cluster_ref="x", prometheus_url="http://169.254.169.254", client=LabProm(), now=NOW)
    assert "no válida" in snap["meta"]["aborted"] and snap["meta"]["queries"] == 0


def test_queries_are_read_only_instant_queries_and_failures_are_counted():
    prom = LabProm(fail={"hpa"})
    snap = snapshot(client=prom)
    assert snap["meta"]["queries_failed"] == 1 and snap["partial"] is True and "INCOMPLETA" not in k8s_lab.render_report(snap)
    assert all(q for q in prom.queries) and snap["meta"]["queries"] == len(prom.queries) + 1   # + la comprobación inicial count(up)


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(k8s_lab, "collect_snapshot", lambda **kw: snapshot())
    assert cli.main(["k8s-lab", "--out-dir", str(tmp_path / "ok")]) == 0
    assert (tmp_path / "ok" / "report.md").exists()
    monkeypatch.setattr(k8s_lab, "collect_snapshot", lambda **kw: snapshot(client=LabProm(down=True)))
    assert cli.main(["k8s-lab", "--out-dir", str(tmp_path / "ko")]) == 1


def test_lab_manifests_match_expectations_and_are_valid_yaml():
    docs = [d for d in yaml.safe_load_all(open("infrastructure/k8s-lab/workloads.yaml", encoding="utf-8")) if d]
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"}
    assert names == set(k8s_lab.LAB_EXPECTATIONS)
    assert any(d["kind"] == "HorizontalPodAutoscaler" and d["spec"]["scaleTargetRef"]["name"] == "hpa-protected" for d in docs)
    assert all(d["metadata"].get("namespace", "lab-dev") == "lab-dev" for d in docs if d["kind"] != "Namespace")
    assert next(d for d in docs if d["kind"] == "Namespace")["metadata"]["name"] == "lab-dev" and "lab-dev" not in SYSTEM_NAMESPACES
