"""Clúster de Kubernetes de extremo a extremo contra PostgreSQL real: alta de la cuenta, escaneo con un Prometheus simulado,
hallazgo con parche de values.yaml (proveedor Git «local»), aprobación y Pull Request. Se omite sin DATABASE_URL / DATABASE_ADMIN_URL."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

GIB, MIB = 2**30, 2**20
NOW = time.time()
VALUES = '''# values del chart de checkout
resources:
  requests:
    cpu: "1"
    memory: 2Gi
  limits:
    cpu: "1"
    memory: 2Gi
'''


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def _row(value, **labels):
    return {"metric": labels, "value": [NOW, str(value)]}


class _Prom:
    """Un Deployment `checkout` (2 réplicas) que pide 1 core / 2 GiB y usa muy poco."""

    def get_json(self, url, params=None):
        q = params["query"]
        pods = [("co-1", "app"), ("co-2", "app")]

        def per_pod(value):
            return [_row(value, namespace="shop-prod", pod=p, container=c) for p, c in pods]

        if "kube_pod_container_resource_requests" in q:
            rows = per_pod(1 if 'resource="cpu"' in q else 2 * GIB)
        elif "kube_pod_container_resource_limits" in q:
            rows = per_pod(1 if 'resource="cpu"' in q else 2 * GIB)
        elif "quantile_over_time" in q:
            rows = per_pod(.2)
        elif "max_over_time(sum by" in q:
            rows = per_pod(.3)
        elif "avg_over_time(sum by" in q:
            rows = per_pod(.1)
        elif "max_over_time(container_memory" in q:
            rows = per_pod(600 * MIB)
        elif "avg_over_time(max by" in q:
            rows = per_pod(400 * MIB)
        elif "kube_pod_owner" in q:
            rows = [_row(1, namespace="shop-prod", pod=p, owner_kind="ReplicaSet", owner_name="checkout-7d9") for p, _ in pods]
        elif "kube_replicaset_owner" in q:
            rows = [_row(1, namespace="shop-prod", replicaset="checkout-7d9", owner_kind="Deployment", owner_name="checkout")]
        elif "kube_deployment_created" in q:
            rows = [_row(NOW - 30 * 86400, namespace="shop-prod", deployment="checkout")]
        elif "timestamp(up)" in q:
            rows = [{"metric": {}, "value": [NOW, str(15 * 86400)]}]
        else:                                            # OOM, HPA, etiquetas de Helm, StatefulSet/DaemonSet: nada
            rows = []
        return {"status": "success", "data": {"resultType": "vector", "result": rows}}


def test_kubernetes_cluster_scan_patch_and_pull_request():
    _require_db()
    import psycopg
    import yaml
    from cloudcost.collectors.kubernetes import KubernetesCollector
    from cloudcost.config import Settings
    from cloudcost.db import tenant_tx
    from cloudcost.routers.admin import create_account
    from cloudcost.schemas import CloudAccountIn
    from cloudcost.secrets import SecretResolver
    from cloudcost.security import Principal
    from cloudcost.services import recommendations as recs
    from cloudcost.services import scan_service, workflow

    suffix = uuid4().hex[:6]
    org = uuid4()
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as conn:
        conn.execute("insert into organizations (id, name, slug) values (%s, 'K8s', %s)", (str(org), f"k8s-{suffix}"))
    iac_dir, pr_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
    (Path(iac_dir) / "charts/checkout").mkdir(parents=True)
    (Path(iac_dir) / "charts/checkout/values.yaml").write_text(VALUES)
    (Path(iac_dir) / "charts/checkout/Chart.yaml").write_text("apiVersion: v2\nname: checkout\nversion: 1.0.0\n")
    settings, secrets = Settings(demo_enabled=True, demo_iac_dir=iac_dir, demo_pr_dir=pr_dir), SecretResolver()

    admin = Principal(user_id="test:admin", email="admin@example.com", org_id=org, role="ADMIN")
    finops = Principal(user_id="test:ana", email="ana@example.com", org_id=org, role="FINOPS")
    body = CloudAccountIn(provider="kubernetes", account_ref=f"eks-{suffix}", display_name="EKS prod",
                          prometheus_url="http://prometheus.monitoring.svc:9090")
    account = create_account(body, admin, settings)
    assert account["provider"] == "kubernetes"
    with tenant_tx(org) as conn:
        repo_id = conn.execute("insert into repositories (organization_id, provider, full_name) values (%s, 'local', 'acme/gitops') returning id",
                               (str(org),)).fetchone()["id"]
        scan_id = conn.execute("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) values (%s, %s, %s, 'test') returning id",
                               (str(org), str(account["id"]), str(repo_id))).fetchone()["id"]

    stats = scan_service.run_scan(org, scan_id, settings=settings, secrets=secrets,
                                  collector_factory=lambda acct, **kw: KubernetesCollector(acct, kw["secrets"], client=_Prom()))
    assert stats["resources_seen"] == 1 and stats["findings"] == 1 and not stats["partial"], stats

    with tenant_tx(org) as conn:
        res = conn.execute("select provider, service, iac_address, attributes from resources where cloud_account_id = %s", (str(account["id"]),)).fetchone()
        (item,) = recs.list_recommendations(conn)["items"]
        detail = recs.get_detail(conn, item["id"])
    assert (res["provider"], res["service"]) == ("kubernetes", "k8s_workload")
    assert res["iac_address"] == "charts/checkout/values.yaml#resources" and res["attributes"]["replicas"] == 2
    assert item["status"] == "PENDING_APPROVAL" and item["risk"] == "MEDIUM" and item["approvals_required"] == 1
    assert detail["params"]["target"] == {"cpu": "260m", "memory": "752Mi"}                  # máx(p95 .2×1,3 ; máx .3×.8) ; 600 Mi×1,25 → 752 Mi
    assert "memory: 752Mi" in detail["evidence"]["patch"]["diff"] and "patch_error" not in detail["evidence"]

    with tenant_tx(org) as conn:
        assert workflow.decide(conn, finops, item["id"], "APPROVED", "uso real muy por debajo de lo pedido", item["version"])["status"] == "APPROVED"
    with tenant_tx(org) as conn:
        pr = workflow.create_pull_request(conn, finops, item["id"], settings=settings, secrets=secrets)
    assert pr["created"] and "+    memory: 752Mi" in pr["diff"]
    (patched,) = [p for p in Path(pr_dir).rglob("values.yaml")]
    assert yaml.safe_load(patched.read_text())["resources"] == {"requests": {"cpu": "260m", "memory": "752Mi"},
                                                                 "limits": {"cpu": "260m", "memory": "752Mi"}}
    assert "# values del chart de checkout" in patched.read_text()
    assert (Path(iac_dir) / "charts/checkout/values.yaml").read_text() == VALUES            # el original no se toca
