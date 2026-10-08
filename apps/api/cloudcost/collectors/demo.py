"""Colector sintético para desarrollo/demos. Sus recursos coinciden con infrastructure/terraform/example-iac."""
from __future__ import annotations

from datetime import date, timedelta

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import (
    DAYS_PER_MONTH,
    instance_monthly_cost,
    rds_monthly_cost,
    snapshot_monthly_cost,
    volume_monthly_cost,
)
from .base import CollectionResult, CostRecord

REGION = "us-east-1"


def _ec2(rid, name, itype, env, cpu, cpu_max, mem, age):
    tags = {"Name": name, "Environment": env}
    return NormalizedResource(
        "aws", "compute", "ec2", rid, REGION, name=name, environment=environment_from_tags(tags),
        instance_type=itype, state="running", monthly_cost=instance_monthly_cost(itype) or 0.0, cost_source="demo",
        cpu_avg=cpu, cpu_max=cpu_max, memory_avg=mem, age_days=age, observation_days=30, tags=tags)


def _vol(rid, name, vtype, size, env, attached, unattached_days=None):
    tags = {"Name": name, "Environment": env}
    return NormalizedResource(
        "aws", "storage", "ebs", rid, REGION, name=name, environment=environment_from_tags(tags), volume_type=vtype,
        state="in-use" if attached else "available", monthly_cost=volume_monthly_cost(vtype, size), cost_source="demo",
        attached=attached, size_gb=size, age_days=300, observation_days=30, unattached_days=unattached_days, tags=tags)


def _snap(rid, name, size, age, env, extra_tags=None, attributes=None):
    tags = {"Environment": env, **({"Name": name} if name else {}), **(extra_tags or {})}
    return NormalizedResource(
        "aws", "storage", "ebs_snapshot", rid, REGION, name=name, environment=environment_from_tags(tags),
        monthly_cost=snapshot_monthly_cost(size), cost_source="demo", size_gb=size, age_days=age, tags=tags,
        attributes=attributes or {})


def _rds(rid, name, klass, storage, env, conn_max, conn_avg, cpu, cluster=None, engine="aurora-postgresql", multi_az=False):
    tags = {"Name": name, "Environment": env, **({"aws:rds:cluster": cluster} if cluster else {})}
    return NormalizedResource(
        "aws", "database", "rds", rid, REGION, name=name, environment=environment_from_tags(tags), instance_type=klass,
        state="available", monthly_cost=rds_monthly_cost(klass, storage, multi_az), cost_source="demo", cpu_avg=cpu,
        cpu_max=cpu * 3, size_gb=storage, age_days=520, observation_days=45, tags=tags,
        attributes={"engine": engine, "connections_max": conn_max, "connections_avg": conn_avg, "cluster_id": cluster,
                    "multi_az": multi_az})


def _failed_deploy_volumes():
    """Despliegue blue/green que falló a medias: cada intento dejó su volumen de datos sin adjuntar."""
    stack = {"aws:cloudformation:stack-name": "checkout-v2-rollout"}
    out = []
    for n, (size, days) in enumerate([(500, 62), (500, 48), (500, 33), (1000, 21)], start=1):
        v = _vol(f"vol-0demo0failed0{n}", f"checkout-v2-rollout-data-{n}", "gp3", size, "staging", False, days)
        v.tags.update(stack)
        out.append(v)
    return out


def _orphan_pvc_volumes():
    """Volúmenes de PersistentVolumeClaims cuyo namespace se borró (el CSI no los elimina si la política es Retain)."""
    out = []
    for n, (ns, size, days) in enumerate([("feature-checkout-ab", 200, 40), ("load-test-q3", 400, 77)], start=1):
        v = _vol(f"vol-0demo0pvc00000{n}", f"pvc-{ns}-data", "gp2", size, "development", False, days)
        v.tags.update({"kubernetes.io/created-for/pvc/namespace": ns, "kubernetes.io/created-for/pvc/name": "data"})
        out.append(v)
    return out


class DemoCollector:
    def collect(self) -> CollectionResult:
        resources = [
            _ec2("i-0demo00000000web1", "web-prod-1", "m5.2xlarge", "production", 12.4, 41.0, 19.8, 400),
            _ec2("i-0demo00000000api1", "api-prod-1", "t3.large", "production", 38.0, 71.0, 55.0, 400),
            _ec2("i-0demo0000000batch", "batch-dev-1", "c5.4xlarge", "development", 4.2, 22.0, 11.0, 200),
            _ec2("i-0demo00000reports", "reports-stg-1", "r5.xlarge", "staging", 1.2, 4.1, 6.0, 150),
            _vol("vol-0demo000apidata", "api-data-prod", "gp3", 100, "production", True),
            _vol("vol-0demo0000legacy", "legacy-data-prod", "gp2", 500, "production", False, 45),
            _vol("vol-0demo000scratch", "scratch-dev", "gp3", 200, "development", False, 30),
            _snap("snap-0demo0oldbackup", "old-backup-dev", 300, 210, "development"),
            _snap("snap-0demo00legalhold", "audit-2021-snapshot", 120, 900, "production", {"retain": "true"}),
            _snap("snap-0demo00amibacked", None, 80, 400, "production", attributes={"ami_ids": ["ami-0demo"]}),
            # --- Incidente de migración: el clúster Aurora anterior sigue encendido y sin tráfico desde el cambio de región
            _rds("db-0demo0ordersw", "orders-legacy-writer", "db.r5.2xlarge", 500, "production", 0, 0.0, 0.9, "orders-legacy"),
            _rds("db-0demo0ordersr", "orders-legacy-reader", "db.r5.xlarge", 500, "production", 0, 0.0, 0.6, "orders-legacy"),
            _rds("db-0demo0reportsd", "reports-pg-dev", "db.m5.large", 100, "development", 1, 0.1, 1.4, None, "postgres"),
            _rds("db-0demo0billing", "billing-prod", "db.r5.2xlarge", 800, "production", 140, 62.0, 34.0, None, "postgres", True),
            *_failed_deploy_volumes(),
            *_orphan_pvc_volumes(),
        ]
        today = date.today()
        costs = [
            CostRecord(r.resource_id, today - timedelta(days=d), round(r.monthly_cost / DAYS_PER_MONTH, 4),
                       r.service, r.region, "demo")
            for r in resources for d in range(1, 15)
        ]
        return CollectionResult(resources=resources, costs=costs, warnings=["Datos sintéticos (modo demo)"])


def expected_demo_summary(findings) -> dict:
    """Resumen legible del escenario (usado por docs y pruebas)."""
    return {"findings": len(findings), "monthly_savings": round(sum(f.estimated_monthly_savings for f in findings), 2)}
