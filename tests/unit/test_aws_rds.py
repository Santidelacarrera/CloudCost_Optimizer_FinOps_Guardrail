"""Inventario de RDS (opt-in) con respuestas validadas por el modelo real de la API: base abandonada, Aurora omitida, permisos y paginación."""
from __future__ import annotations

from aws_stub import ACCOUNT, TODAY, StubbedSession, describe_instances, metric_data, rds_instance
from cloudcost.collectors.aws import AwsCollector
from cloudcost.domain.rules import RuleConfig, evaluate_all


class _Secrets:
    def resolve(self, ref, org_id=None):
        return None


def _collect(session, **kw):
    c = AwsCollector({"regions": ["us-east-1"], "role_arn": None, "account_ref": ACCOUNT}, _Secrets(), use_cost_explorer=False, session=session,
                     sleep=lambda s: None, today=lambda: TODAY, **kw)
    return c.collect()


def _base(session):
    session.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "x", "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/x/y"})
    session.respond("ec2", "DescribeInstances", describe_instances())
    session.respond("ec2", "DescribeVolumes", {"Volumes": []})
    session.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    return session


def _idle(ident="db-orders", n=0):
    return [(f"r{n}_0", [0.5] * 336), (f"r{n}_1", [1.0] * 336), (f"r{n}_2", [0.0] * 336), (f"r{n}_3", [0.0] * 336)]


def test_rds_is_off_by_default_and_makes_no_rds_call():
    s = _base(StubbedSession())
    result = _collect(s)
    assert not any(svc == "rds" for svc, _ in s.calls) and result.resources == []


def test_idle_database_is_inventoried_with_metrics_and_triggers_the_abandoned_database_rule():
    s = _base(StubbedSession())
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [rds_instance("db-orders", tags={"Environment": "development", "Name": "orders"})]})
    s.respond("cloudwatch", "GetMetricData", metric_data(*_idle()))
    result = _collect(s, include_rds=True)
    s.assert_all_consumed()
    (db,) = result.resources
    assert (db.provider, db.service, db.resource_id, db.instance_type, db.state, db.environment) == ("aws", "rds", "db-orders", "db.m5.large", "available", "development")
    assert db.cpu_avg == 0.5 and db.attributes["connections_avg"] == 0 and db.attributes["connections_max"] == 0 and db.observation_days == 14
    assert db.size_gb == 200 and db.monthly_cost > 100 and db.attributes["engine"] == "postgres" and db.attributes["deletion_protection"] is False
    (finding,) = evaluate_all(result.resources, RuleConfig())
    assert finding.rule_id == "rds_idle" and finding.destructive is True and finding.estimated_monthly_savings == round(db.monthly_cost, 2)


def test_busy_database_is_not_proposed_for_deletion():
    s = _base(StubbedSession())
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [rds_instance()]})
    s.respond("cloudwatch", "GetMetricData", metric_data(("r0_0", [35.0] * 336), ("r0_1", [80.0] * 336), ("r0_2", [14.0] * 336), ("r0_3", [40.0] * 336)))
    assert evaluate_all(_collect(s, include_rds=True).resources, RuleConfig()) == []


def test_aurora_cluster_members_and_unavailable_databases_are_not_evaluated():
    s = _base(StubbedSession())
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [
        rds_instance("aur-1", engine="aurora-postgresql", DBClusterIdentifier="cluster-a"), rds_instance("stopped-db", status="stopped"),
        rds_instance("ok-db")]})
    s.respond("cloudwatch", "GetMetricData", metric_data(*_idle()))
    result = _collect(s, include_rds=True)
    assert [r.resource_id for r in result.resources] == ["ok-db"]
    assert any("Aurora/clúster" in w for w in result.warnings)


def test_missing_rds_permission_is_classified_and_marks_the_scan_partial_without_leaking_aws_text():
    s = _base(StubbedSession())
    s.fail("rds", "DescribeDBInstances", "AccessDenied", "User: arn:aws:sts::111122223333:assumed-role/secret-role-name/x is not authorized", status=403)
    result = _collect(s, include_rds=True)
    assert result.partial and result.resources == []
    issue = next(i for i in result.issues if i["api"].startswith("rds:"))
    assert issue["kind"] == "permission_denied" and "secret-role-name" not in " ".join(result.warnings)


def test_pagination_collects_every_page():
    s = _base(StubbedSession())
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [rds_instance("db-1")], "Marker": "m1"})
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [rds_instance("db-2")]}, expected={"Marker": "m1"})
    s.respond("cloudwatch", "GetMetricData", metric_data(*_idle(n=0), *_idle(n=1)))
    assert sorted(r.resource_id for r in _collect(s, include_rds=True).resources) == ["db-1", "db-2"]


def test_lab_snapshot_with_include_rds_records_the_rds_operation():
    from aws_stub import ACCOUNT as ACC
    from cloudcost import lab

    s = _base(StubbedSession())
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [rds_instance()]})
    s.respond("cloudwatch", "GetMetricData", metric_data(*_idle()))
    snap = lab.collect_snapshot(lab.LabConfig(account_ref=ACC, regions=["us-east-1"], use_cost_explorer=False, include_rds=True, today=TODAY,
                                              sleep=lambda x: None), session=s)
    assert ("rds", "DescribeDBInstances") in {(c["service"], c["operation"]) for c in snap["api_calls"]}
    assert [r["resource_id"] for r in snap["resources"]] == ["db-orders"] and [f["rule_id"] for f in snap["findings"]] == ["rds_idle"]
