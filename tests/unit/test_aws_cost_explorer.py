"""Cost Explorer nativo: costo real por recurso (ARN/ID), historial por etiqueta y degradación segura. Sin AWS real: cliente simulado."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from cloudcost.collectors import aws_costs
from cloudcost.collectors.aws import AwsCollector
from cloudcost.domain.models import NormalizedResource
from cloudcost.domain.rules import RuleConfig, evaluate_all
from cloudcost.services.explain import build_explanation

TODAY = date(2026, 10, 8)


def _bucket(day: date, groups: list[tuple[str, float]], metric="UnblendedCost"):
    return {"TimePeriod": {"Start": day.isoformat(), "End": (day + timedelta(days=1)).isoformat()},
            "Groups": [{"Keys": [k], "Metrics": {metric: {"Amount": str(a), "Unit": "USD"}}} for k, a in groups]}


class FakeCE:
    """get_cost_and_usage_with_resources paginado en 2 páginas + get_cost_and_usage mensual por TAG."""

    def __init__(self, fail_resources=False, fail_tag=False, empty=False):
        self.fail_resources, self.fail_tag, self.empty = fail_resources, fail_tag, empty
        self.calls: list[tuple[str, dict]] = []

    def get_cost_and_usage_with_resources(self, **kw):
        self.calls.append(("resources", kw))
        if self.fail_resources:
            raise RuntimeError("AccessDeniedException")
        if self.empty:
            return {"ResultsByTime": []}
        start = date.fromisoformat(kw["TimePeriod"]["Start"])
        days = [start + timedelta(days=i) for i in range(14)]
        if "NextPageToken" not in kw:
            # página 1: la instancia como ARN (AWS puede devolver ARN o ID según el servicio) y un volumen
            buckets = [_bucket(d, [("arn:aws:ec2:us-east-1:111122223333:instance/i-big", 10.0),
                                   ("vol-orphan", 2.0), ("NoResourceId", 99.0)]) for d in days[:7]]
            return {"ResultsByTime": buckets, "NextPageToken": "p2"}
        buckets = [_bucket(d, [("arn:aws:ec2:us-east-1:111122223333:instance/i-big", 10.0), ("vol-orphan", 2.0)])
                   for d in days[7:]]
        return {"ResultsByTime": buckets}

    def get_cost_and_usage(self, **kw):
        self.calls.append(("tag", kw))
        if self.fail_tag:
            raise RuntimeError("DataUnavailableException")
        key = kw["GroupBy"][0]["Key"]
        return {"ResultsByTime": [
            {"TimePeriod": {"Start": "2026-05-01", "End": "2026-06-01"},
             "Groups": [{"Keys": [f"{key}$checkout"], "Metrics": {"UnblendedCost": {"Amount": "100.0"}}},
                        {"Keys": [f"{key}$"], "Metrics": {"UnblendedCost": {"Amount": "5.0"}}}]},
            {"TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
             "Groups": [{"Keys": [f"{key}$checkout"], "Metrics": {"UnblendedCost": {"Amount": "150.0"}}}]}]}


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kw):
        return iter(self.pages)


class FakeEC2:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.pages = {
            "describe_instances": [{"Reservations": [{"Instances": [{
                "InstanceId": "i-big", "InstanceType": "m5.2xlarge", "State": {"Name": "running"},
                "LaunchTime": now - timedelta(days=60), "Tags": [{"Key": "Name", "Value": "checkout"}, {"Key": "env", "Value": "dev"}]}]}]}],
            "describe_volumes": [{"Volumes": [{
                "VolumeId": "vol-orphan", "State": "in-use", "Size": 500, "VolumeType": "gp3",
                "Attachments": [{"InstanceId": "i-big"}], "CreateTime": now - timedelta(days=200), "Tags": []}]}],
            "describe_snapshots": [{"Snapshots": []}],
        }

    def get_paginator(self, name):
        return _Paginator(self.pages[name])

    def describe_images(self, **kw):
        return {"Images": []}


class FakeCW:
    def get_paginator(self, name):
        return _Paginator([{"Metrics": []}])

    def get_metric_data(self, **kw):
        out = []
        for q in kw["MetricDataQueries"]:
            out.append({"Id": q["Id"], "Values": [1.0] * 24 * 14})
        return {"MetricDataResults": out}


class FakeSession:
    def __init__(self, ce):
        self.ce = ce

    def client(self, name, region_name=None):
        return {"ec2": FakeEC2(), "cloudwatch": FakeCW(), "cloudtrail": object(), "ce": self.ce}[name]


class _Secrets:
    def resolve(self, ref, org_id=None):
        return None


def _collector(ce, **kw):
    errors: list[str] = []
    c = AwsCollector({"regions": ["us-east-1"], "role_arn": None}, _Secrets(), use_cost_explorer=True,
                     on_api_error=errors.append, session=FakeSession(ce), **kw)
    return c, errors


# --------------------------------------------------------------------------- unidades puras
def test_normalize_resource_id_handles_arns_and_plain_ids():
    assert aws_costs.normalize_resource_id("arn:aws:ec2:us-east-1:111122223333:instance/i-0abc") == "i-0abc"
    assert aws_costs.normalize_resource_id("arn:aws:ec2:us-east-1:111122223333:volume/vol-9") == "vol-9"
    assert aws_costs.normalize_resource_id("vol-123") == "vol-123"
    assert aws_costs.normalize_resource_id("") == ""


def test_monthly_estimate_uses_resource_age_when_younger_than_window():
    cost = aws_costs.ResourceCost([(TODAY - timedelta(days=i), 4.0) for i in range(1, 6)])   # 5 días de datos
    assert cost.monthly_estimate(30.0) == pytest.approx(120.0)
    assert cost.monthly_estimate(30.0, age_days=3) == pytest.approx(5 * 4.0 / 3 * 30.0, rel=1e-3)


def test_fetch_resource_costs_paginates_and_skips_no_resource_id():
    ce = FakeCE()
    out = aws_costs.fetch_resource_costs(ce, today=TODAY)
    assert set(out) == {"i-big", "vol-orphan"}
    assert out["i-big"].days_with_data == 14
    assert [c[0] for c in ce.calls] == ["resources", "resources"]
    assert ce.calls[0][1]["TimePeriod"] == {"Start": "2026-09-24", "End": "2026-10-08"}      # 14 días, nunca más
    assert ce.calls[0][1]["Granularity"] == "DAILY"


def test_fetch_resource_costs_caps_window_at_14_days_and_rejects_unknown_metric():
    ce = FakeCE()
    aws_costs.fetch_resource_costs(ce, today=TODAY, window_days=90, metric="Bogus")
    kw = ce.calls[0][1]
    assert kw["TimePeriod"]["Start"] == "2026-09-24"
    assert kw["Metrics"] == ["UnblendedCost"]


def test_fetch_tag_history_is_monthly_sorted_and_excludes_untagged():
    ce = FakeCE()
    out = aws_costs.fetch_tag_history(ce, "Name", months=6, today=TODAY)
    assert out == {"checkout": [("2026-05", 100.0), ("2026-09", 150.0)]}
    period = ce.calls[0][1]["TimePeriod"]
    assert period == {"Start": "2026-04-01", "End": "2026-10-01"}            # 6 meses completos, sin el mes en curso
    assert ce.calls[0][1]["Granularity"] == "MONTHLY"
    assert aws_costs.trend_pct(out["checkout"]) == 50.0
    assert aws_costs.trend_pct([("2026-05", 10.0)]) is None


def test_fetch_tag_history_clamps_months_to_12_across_year_boundary():
    ce = FakeCE()
    aws_costs.fetch_tag_history(ce, "app", months=99, today=date(2026, 2, 10))
    assert ce.calls[0][1]["TimePeriod"] == {"Start": "2025-02-01", "End": "2026-02-01"}


# --------------------------------------------------------------------------- colector
def test_collector_replaces_estimates_with_real_costs():
    c, errors = _collector(FakeCE())
    result = c.collect()
    by_id = {r.resource_id: r for r in result.resources}
    inst = by_id["i-big"]
    assert inst.cost_source == "cost_explorer"
    assert inst.monthly_cost == pytest.approx(10.0 * 30.4375, rel=1e-3)               # costo real, no la tabla (m5.2xlarge ≈ 280)
    assert inst.attributes["cost_basis"]["window_days"] == 14
    assert by_id["vol-orphan"].monthly_cost == pytest.approx(2.0 * 30.4375, rel=1e-3)
    real = [r for r in result.costs if r.source == "cost_explorer"]
    assert len(real) == 28 and not errors and not result.partial


def test_collector_attaches_tag_history_without_assigning_it_to_a_single_resource():
    c, _ = _collector(FakeCE(), cost_tag_key="Name", cost_history_months=6)
    inst = next(r for r in c.collect().resources if r.resource_id == "i-big")
    hist = inst.attributes["cost_history"]
    assert hist["scope"] == "tag" and hist["tag_value"] == "checkout"
    assert hist["trend_pct"] == 50.0 and "agregado" in hist["note"]
    assert "cost_history" not in next(r for r in c.collect().resources if r.resource_id == "vol-orphan").attributes


def test_ce_failure_degrades_to_estimate_without_marking_inventory_partial():
    c, errors = _collector(FakeCE(fail_resources=True, fail_tag=True), cost_tag_key="Name")
    result = c.collect()
    inst = next(r for r in result.resources if r.resource_id == "i-big")
    assert inst.cost_source == "estimate" and inst.monthly_cost > 200                    # tabla de precios
    assert result.partial is False                                                        # el inventario sí está completo
    assert "ce:GetCostAndUsageWithResources" in errors and "ce:GetCostAndUsage" in errors
    assert any("GetCostAndUsageWithResources" in w for w in result.warnings)


def test_ce_empty_data_warns_and_keeps_estimates():
    c, _ = _collector(FakeCE(empty=True))
    result = c.collect()
    assert all(r.cost_source != "cost_explorer" for r in result.resources)
    assert any("sin datos por recurso" in w for w in result.warnings)


def test_ce_disabled_makes_no_ce_calls():
    ce = FakeCE()
    c = AwsCollector({"regions": ["us-east-1"]}, _Secrets(), use_cost_explorer=False, session=FakeSession(ce))
    c.collect()
    assert ce.calls == []


# --------------------------------------------------------------------------- reglas
def _idle(source: str) -> NormalizedResource:
    return NormalizedResource("aws", "compute", "ec2", "i-x", "us-east-1", name="x", environment="development",
                              instance_type="m5.2xlarge", state="running", monthly_cost=300.0, cost_source=source,
                              cpu_avg=1.0, cpu_max=4.0, memory_avg=10.0, observation_days=20)


def test_findings_carry_cost_basis_and_explanation_distinguishes_real_from_estimated():
    from cloudcost.domain.policy import PolicyConfig, evaluate
    from cloudcost.domain.risk import classify_risk

    for source, verified, phrase in (("cost_explorer", True, "Costo real según Cost Explorer"), ("estimate", False, "Costo ESTIMADO")):
        f = evaluate_all([_idle(source)], RuleConfig())[0]
        assert f.evidence["cost_basis"]["verified"] is verified
        risk = classify_risk(f)
        text = build_explanation(f, risk, evaluate(action=f.action, risk=risk, environment="development",
                                                   confidence=f.confidence, cfg=PolicyConfig()))
        assert phrase in text


def test_require_verified_cost_blocks_findings_built_on_price_table_estimates():
    strict = RuleConfig.from_overrides({"require_verified_cost": True})
    assert evaluate_all([_idle("estimate")], strict) == []
    assert evaluate_all([_idle("cost_explorer")], strict) != []
    assert evaluate_all([_idle("estimate")], RuleConfig()) != []                        # por defecto sí se propone, marcado


def test_savings_use_real_cost_not_price_table():
    res = _idle("cost_explorer")
    res.monthly_cost = 90.0                                                              # p. ej. con descuentos reales
    f = evaluate_all([res], RuleConfig())[0]
    assert f.current_monthly_cost == 90.0 and f.estimated_monthly_savings == 90.0
