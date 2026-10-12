"""Escenarios SINTÉTICOS de Cost Explorer (sin llamadas reales a AWS) para probar los guardrails de presupuesto de punta a punta.

Las respuestas pasan por el Stubber de botocore (ver aws_stub.py): se validan contra el modelo real de la API. Todos los datos son
inventados: cuenta 111122223333, importes redondos, ningún identificador de cliente real.

Escenario base («happy»): 4 solicitudes a Cost Explorer por escaneo (2 páginas por recurso + diario + mensual) = USD 0,04.
Gasto diario de la cuenta: 12,0 (EC2) + 1,0 (S3) + 3,0 (EC2-Other) + 0,5 (otro) = USD 16,5 por día.
"""
from __future__ import annotations

from datetime import timedelta

from aws_stub import (
    ACCOUNT,
    TODAY,
    StubbedSession,
    ce_bucket,
    ce_response,
    describe_instances,
    instance,
    metric_data,
    snapshot,
    volume,
)
from botocore.stub import ANY
from cloudcost.collectors import aws_costs
from cloudcost.collectors.aws import AwsCollector

CE_REQUESTS_PER_SCAN = 4
DAILY_ACCOUNT_SPEND = 16.5
CE_REQUEST_USD = 0.01


class NoSecrets:
    def resolve(self, ref, org_id=None):
        return None


def _days(n: int = 14):
    return [TODAY - timedelta(days=i) for i in range(n, 0, -1)]


def inventory(session: StubbedSession) -> None:
    session.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "AROAEXAMPLE:cloudcost", "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/x/y"})
    session.respond("ec2", "DescribeInstances", describe_instances(instance("i-0aaa")))
    session.respond("ec2", "DescribeVolumes", {"Volumes": [volume("vol-0aaa"), volume("vol-0orph", state="available", attached=None)]})
    session.respond("ec2", "DescribeImages", {"Images": []})
    session.respond("ec2", "DescribeSnapshots", {"Snapshots": [snapshot("snap-0old")]})
    session.respond("cloudwatch", "ListMetrics", {"Metrics": []})
    session.respond("cloudwatch", "GetMetricData", metric_data(("c0a", [2.0] * 336), ("c0x", [8.0] * 336)))
    session.respond("cloudtrail", "LookupEvents", {"Events": []})


def cost_explorer(session: StubbedSession) -> None:
    rows = [(["arn:aws:ec2:us-east-1:111122223333:instance/i-0aaa"], 10.0), (["vol-0aaa"], 2.0), (["vol-0orph"], 1.5)]
    buckets = [ce_bucket(d, rows) for d in _days()]
    session.respond("ce", "GetCostAndUsageWithResources", ce_response(buckets[:7], "p2"))
    session.respond("ce", "GetCostAndUsageWithResources", ce_response(buckets[7:]))
    svc = [(["Amazon Elastic Compute Cloud - Compute", "us-east-1"], 12.0), (["Amazon Simple Storage Service", "NoRegion"], 1.0),
           (["EC2 - Other", "us-east-1"], 3.0), (["Some New Service", "eu-west-1"], 0.5)]
    daily = [ce_bucket(d, svc, estimated=(d == TODAY - timedelta(days=1))) for d in _days(35)]
    months = [ce_bucket(aws_costs.month_start(TODAY, b), [(k, v * 30) for k, v in svc], end=aws_costs.month_start(TODAY, b - 1))
              for b in range(6, 0, -1)]
    session.respond("ce", "GetCostAndUsage", ce_response(daily), expected={
        "TimePeriod": {"Start": (TODAY - timedelta(days=35)).isoformat(), "End": TODAY.isoformat()}, "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"], "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}},
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "REGION"}]})
    session.respond("ce", "GetCostAndUsage", ce_response(months), expected={
        "TimePeriod": {"Start": "2026-04-01", "End": "2026-10-01"}, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
        "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}, "GroupBy": ANY})


def happy_session() -> StubbedSession:
    s = StubbedSession()
    inventory(s)
    cost_explorer(s)
    return s


def aws_factory(session: StubbedSession):
    """collector_factory para scan_service.run_scan: el colector REAL de AWS sobre la sesión simulada (sin red)."""
    built: list[AwsCollector] = []

    def factory(account, *, secrets=None, demo_enabled=False, use_cost_explorer=False, cost_options=None, on_api_error=None):
        opts = dict(cost_options or {})
        c = AwsCollector({**account, "role_arn": None, "account_ref": ACCOUNT, "regions": ["us-east-1"]}, NoSecrets(),
                         use_cost_explorer=use_cost_explorer, on_api_error=on_api_error, session=session, sleep=lambda s: None,
                         today=lambda: TODAY, ce_request_budget=opts.get("ce_request_budget", aws_costs.DEFAULT_CE_BUDGET))
        built.append(c)
        return c

    factory.built = built                                                # type: ignore[attr-defined]
    return factory
