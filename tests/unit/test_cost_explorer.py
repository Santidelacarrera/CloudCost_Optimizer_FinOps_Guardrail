"""Costos reales de AWS: atribución por ID/ARN, por etiqueta, histórico y compuerta 'sin costo real no se propone'."""
from datetime import date

import pytest
from cloudcost.collectors.aws import AwsCollector
from cloudcost.collectors.aws_cost import (
    CostExplorerSource,
    attribute_costs,
    monthly_from_series,
    resource_key,
)
from cloudcost.collectors.base import CollectionResult
from cloudcost.domain.models import NormalizedResource
from cloudcost.domain.rules import RuleConfig, evaluate_all

TODAY = date(2026, 10, 8)


class FakeCE:
    """Imita las tres operaciones de Cost Explorer que usamos, con paginación real."""

    def __init__(self, resources=None, tags=None, monthly=None, fail=()):
        self.resources, self.tags, self.monthly, self.fail = resources or {}, tags or {}, monthly or [], set(fail)
        self.calls: list[tuple[str, dict]] = []

    def _days(self, kw):
        return [date(2026, 10, d) for d in (1, 2)]

    def get_cost_and_usage_with_resources(self, **kw):
        self.calls.append(("with_resources", kw))
        if "with_resources" in self.fail:
            raise RuntimeError("DataUnavailableException")
        assert kw["GroupBy"] == [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}]
        assert "Filter" in kw                                    # la API exige un filtro
        keys = sorted(self.resources)
        page = int(kw.get("NextPageToken") or 0)
        chunk = keys[page * 2:(page + 1) * 2]
        out = {"ResultsByTime": [{"TimePeriod": {"Start": d.isoformat()},
                                  "Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(self.resources[k])}}} for k in chunk]}
                                 for d in self._days(kw)]}
        if (page + 1) * 2 < len(keys):
            out["NextPageToken"] = str(page + 1)
        return out

    def get_cost_and_usage(self, **kw):
        self.calls.append(("usage", kw))
        if "usage" in self.fail:
            raise RuntimeError("AccessDenied")
        g = kw["GroupBy"][0]
        if kw["Granularity"] == "MONTHLY":
            return {"ResultsByTime": [{"TimePeriod": {"Start": m}, "Groups": [
                {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(a)}}} for k, a in rows]} for m, rows in self.monthly]}
        assert g["Type"] == "TAG"
        return {"ResultsByTime": [{"TimePeriod": {"Start": d.isoformat()}, "Groups": [
            {"Keys": [f"{g['Key']}${v}"], "Metrics": {"UnblendedCost": {"Amount": str(a)}}} for v, a in self.tags.items()]}
            for d in self._days(kw)]}


class FakeSession:
    def __init__(self, ce):
        self.ce = ce

    def client(self, name, region_name=None):
        assert name == "ce" and region_name == "us-east-1"
        return self.ce


def res(rid, cost=100.0, tags=None, service="ec2"):
    return NormalizedResource("aws", "compute", service, rid, "us-east-1", monthly_cost=cost, tags=tags or {})


def test_resource_key_accepts_ids_and_arns():
    assert resource_key("i-0abc") == "i-0abc"
    assert resource_key("arn:aws:ec2:us-east-1:111122223333:volume/vol-0abc") == "vol-0abc"
    assert resource_key("arn:aws:rds:us-east-1:111122223333:db:orders") == "orders"
    assert resource_key("arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/web/50dc") == "50dc"


def test_costs_by_resource_id_and_arn_with_pagination():
    ce = FakeCE(resources={"i-1": 10.0, "arn:aws:ec2:us-east-1:111122223333:volume/vol-2": 2.0, "i-3": 5.0, "NoResourceId": 99.0})
    got = CostExplorerSource(FakeSession(ce), today=lambda: TODAY).resource_costs()
    assert set(got) == {"i-1", "vol-2", "i-3"}                           # NoResourceId se descarta; ARN normalizado
    assert got["i-1"] == [(date(2026, 10, 1), 10.0), (date(2026, 10, 2), 10.0)]
    assert len([c for c in ce.calls if c[0] == "with_resources"]) == 2    # dos páginas
    kw = ce.calls[0][1]
    assert kw["TimePeriod"] == {"Start": "2026-09-24", "End": "2026-10-08"}   # ventana de 14 días


def test_window_is_capped_to_what_aws_allows():
    ce = FakeCE(resources={"i-1": 1.0})
    CostExplorerSource(FakeSession(ce), today=lambda: TODAY).resource_costs(days=90)
    assert ce.calls[0][1]["TimePeriod"]["Start"] == "2026-09-24"


def test_attribution_prefers_resource_then_tag_and_splits_shared_tags():
    inventory = [res("i-1", 100, {"Project": "web"}), res("i-2", 100, {"Project": "batch"}),
                 res("i-3", 300, {"Project": "batch"}), res("i-4", 50, {})]
    ce = FakeCE(resources={"i-1": 7.0}, tags={"web": 99.0, "batch": 8.0})
    attr = attribute_costs(inventory, CostExplorerSource(FakeSession(ce), today=lambda: TODAY), tag_key="Project")
    assert attr.source == {"i-1": "cost_explorer", "i-2": "cost_explorer_tag", "i-3": "cost_explorer_tag"}
    assert attr.by_resource["i-1"][0][1] == 7.0                          # el costo por recurso gana a la etiqueta
    # "batch" se reparte según el costo estimado (100 vs 300 -> 25% / 75%)
    assert attr.by_resource["i-2"][0][1] == 2.0 and attr.by_resource["i-3"][0][1] == 6.0
    assert "i-4" not in attr.by_resource                                  # sin dato real: sigue como estimación


def test_cost_explorer_failure_is_a_warning_not_an_exception():
    errors = []
    ce = FakeCE(fail={"with_resources", "usage"})
    src = CostExplorerSource(FakeSession(ce), on_error=lambda api, exc: errors.append(api), today=lambda: TODAY)
    attr = attribute_costs([res("i-1", tags={"Project": "x"})], src, tag_key="Project")
    assert attr.by_resource == {} and len(attr.warnings) == 2
    assert errors == ["ce:GetCostAndUsageWithResources", "ce:GetCostAndUsage"]


def test_monthly_history_by_service_and_tag():
    ce = FakeCE(monthly=[("2026-08-01", [("Amazon EC2", 120.5), ("Amazon RDS", 40.0)]),
                         ("2026-09-01", [("Amazon EC2", 130.0)])])
    rows = CostExplorerSource(FakeSession(ce), today=lambda: TODAY).monthly_history(months=2)
    assert rows[0] == {"month": "2026-08", "key": "Amazon EC2", "amount": 120.5} and len(rows) == 3
    assert ce.calls[0][1]["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-10-08"}
    assert ce.calls[0][1]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
    CostExplorerSource(FakeSession(ce), today=lambda: TODAY).monthly_history(months=99, group_by="TAG", tag_key="Project")
    assert ce.calls[1][1]["TimePeriod"]["Start"] == "2025-09-01"          # tope de 13 meses
    assert ce.calls[1][1]["GroupBy"] == [{"Type": "TAG", "Key": "Project"}]


def test_monthly_from_series():
    assert monthly_from_series([]) == 0.0
    assert monthly_from_series([(date(2026, 10, 1), 10.0), (date(2026, 10, 2), 20.0)]) == pytest.approx(456.56, abs=0.01)


def test_collector_replaces_estimates_with_real_costs():
    inventory = [res("i-1", 280.32), res("i-2", 50.0)]
    result = CollectionResult(resources=inventory)
    coll = AwsCollector({"provider": "aws", "settings": {}}, secrets=None, session=FakeSession(FakeCE(resources={"i-1": 4.0})))
    coll._apply_real_costs(result)
    assert inventory[0].cost_source == "cost_explorer" and inventory[0].monthly_cost == pytest.approx(121.75, abs=0.01)
    assert inventory[0].attributes["cost_window_days"] == 2
    assert inventory[1].cost_source == "estimate" and inventory[1].monthly_cost == 50.0
    assert any("1 recursos sin costo real" in w for w in coll.warnings)
    assert not coll.partial                                              # un hueco de costos no invalida el inventario
    assert {c.source for c in result.costs} == {"cost_explorer"}


def idle(cost_source):
    return NormalizedResource("aws", "compute", "ec2", "i-idle", "us-east-1", state="running", instance_type="m5.2xlarge",
                              cpu_avg=1.0, cpu_max=4.0, memory_avg=10.0, observation_days=30, environment="development",
                              monthly_cost=280.32, cost_source=cost_source)


def test_gate_blocks_destructive_proposals_without_real_cost():
    skipped = []
    assert evaluate_all([idle("estimate")], RuleConfig(require_real_cost=True), skipped) == []
    assert [f.rule_id for f in skipped] == ["ec2_idle"]
    (f,) = evaluate_all([idle("cost_explorer")], RuleConfig(require_real_cost=True))
    assert f.evidence["cost_basis"] == {"source": "cost_explorer", "verified": True, "monthly_cost": 280.32}


def test_without_the_gate_estimates_still_flow_but_are_labelled():
    (f,) = evaluate_all([idle("estimate")], RuleConfig())
    assert f.evidence["cost_basis"]["verified"] is False and f.evidence["cost_basis"]["source"] == "estimate"


def test_gate_is_configurable_per_organization():
    assert RuleConfig.from_overrides({"require_real_cost": True}).require_real_cost is True


def test_boolean_overrides_are_parsed_not_truthy():
    assert RuleConfig.from_overrides({"require_real_cost": "false"}).require_real_cost is False
    assert RuleConfig.from_overrides({"require_real_cost": "true"}).require_real_cost is True
    assert RuleConfig.from_overrides({"require_real_cost": "quizás"}).require_real_cost is False   # inválido: valor por defecto


# ---------------------------------------------------------------- inventario RDS del colector real
class FakeRDS:
    def get_paginator(self, name):
        assert name == "describe_db_instances"

        class P:
            def paginate(self_inner):
                from datetime import datetime, timedelta, timezone
                old = datetime.now(timezone.utc) - timedelta(days=400)
                return [{"DBInstances": [
                    {"DBInstanceIdentifier": "orders-legacy-writer", "DBInstanceClass": "db.r5.2xlarge", "DBInstanceStatus": "available",
                     "AllocatedStorage": 500, "MultiAZ": False, "Engine": "aurora-postgresql", "DBClusterIdentifier": "orders-legacy",
                     "InstanceCreateTime": old, "DBInstanceArn": "arn:aws:rds:us-east-1:1:db:orders-legacy-writer",
                     "TagList": [{"Key": "Environment", "Value": "prod"}]},
                    {"DBInstanceIdentifier": "stopped-one", "DBInstanceClass": "db.t3.medium", "DBInstanceStatus": "stopped",
                     "AllocatedStorage": 20, "InstanceCreateTime": old, "TagList": []}]}]
        return P()


class FakeCW:
    def get_metric_data(self, **kw):
        ids = {q["Id"]: q["MetricStat"] for q in kw["MetricDataQueries"]}
        assert all(q["MetricStat"]["Metric"]["Namespace"] == "AWS/RDS" for q in kw["MetricDataQueries"])
        vals = {"c0": [0.5] * 336, "x0": [0.0] * 336, "a0": [0.0] * 336}
        return {"MetricDataResults": [{"Id": i, "Values": vals[i]} for i in ids]}


def test_collector_inventories_databases_with_connection_metrics():
    coll = AwsCollector({"provider": "aws"}, secrets=None, session=None)
    dbs = coll._databases(FakeRDS(), FakeCW(), "us-east-1")
    writer = next(d for d in dbs if d.resource_id == "orders-legacy-writer")
    assert writer.service == "rds" and writer.resource_type == "database" and writer.environment == "production"
    assert writer.attributes["connections_max"] == 0.0 and writer.attributes["cluster_id"] == "orders-legacy"
    assert writer.cpu_avg == 0.5 and writer.observation_days == 14
    assert writer.monthly_cost == 787.5
    (f,) = evaluate_all([writer], RuleConfig())                          # la regla rds_idle se dispara con datos reales del colector
    assert f.rule_id == "rds_idle"
    stopped = next(d for d in dbs if d.resource_id == "stopped-one")
    assert stopped.state == "stopped" and "connections_max" not in stopped.attributes
