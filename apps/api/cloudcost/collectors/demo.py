"""Colector sintético para desarrollo/demos. Sus recursos coinciden con infrastructure/terraform/example-iac."""
from __future__ import annotations

from datetime import date, timedelta

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import DAYS_PER_MONTH, instance_monthly_cost, snapshot_monthly_cost, volume_monthly_cost
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
        ]
        today = date.today()
        costs = [
            CostRecord(r.resource_id, today - timedelta(days=d), round(r.monthly_cost / DAYS_PER_MONTH, 4),
                       r.service, r.region, "demo")
            for r in resources for d in range(1, 15)
        ]
        return CollectionResult(resources=resources, costs=costs, warnings=["Datos sintéticos (modo demo)"])
