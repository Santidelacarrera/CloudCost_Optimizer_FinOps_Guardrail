from cloudcost.domain import policy, risk, savings
from cloudcost.domain import state_machine as sm
from cloudcost.domain.models import NormalizedResource, environment_from_tags, is_protected
from cloudcost.domain.pricing import instance_monthly_cost, smaller_types
from cloudcost.domain.rules import (
    ACTION_DELETE_SNAPSHOT,
    ACTION_DELETE_VOLUME,
    ACTION_REMOVE,
    ACTION_RESIZE,
    RuleConfig,
    evaluate_resource,
)

CFG = RuleConfig()


def ec2(**kw):
    base = dict(provider="aws", resource_type="compute", service="ec2", resource_id="i-1", region="us-east-1",
                state="running", instance_type="m5.2xlarge", cpu_avg=12.4, cpu_max=30.0, memory_avg=19.8,
                observation_days=30, environment="production", tags={})
    base.update(kw)
    return NormalizedResource(**base)


def test_pricing_ladder():
    assert instance_monthly_cost("m5.2xlarge") == 280.32
    assert [s.name for s in smaller_types("m5.2xlarge", 2)] == ["m5.xlarge", "m5.large"]
    assert smaller_types("m5.large", 2) == []
    assert smaller_types("zz.unknown") == []


def test_downsize_example_from_design_doc():
    (f,) = evaluate_resource(ec2(monthly_cost=280.32), CFG)
    assert f.rule_id == "ec2_downsize" and f.action == ACTION_RESIZE
    # m5.large dejaría la memoria al 79% (> techo 70%): la regla elige el tipo seguro m5.xlarge
    assert f.params == {"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"}
    assert f.estimated_monthly_savings == 140.16
    assert f.projected_monthly_cost == 140.16
    assert 0.8 <= f.confidence <= 0.97
    assert not f.destructive


def test_downsize_requires_observation_memory_and_thresholds():
    assert evaluate_resource(ec2(observation_days=7), CFG) == []
    assert evaluate_resource(ec2(memory_avg=None), CFG) == []
    assert evaluate_resource(ec2(cpu_avg=40.0), CFG) == []
    assert evaluate_resource(ec2(memory_avg=45.0), CFG) == []
    assert evaluate_resource(ec2(state="stopped"), CFG) == []
    assert evaluate_resource(ec2(), RuleConfig(require_memory_metric=False)) != []


def test_downsize_respects_peak_ceiling():
    # pico del 80% sobre 8 vCPU = 6.4 vCPU: ni 4 ni 2 vCPU lo soportan
    assert evaluate_resource(ec2(cpu_max=80.0), CFG) == []


def test_idle_takes_precedence_over_downsize_and_is_destructive():
    (f,) = evaluate_resource(ec2(cpu_avg=1.0, cpu_max=4.0, memory_avg=6.0, environment="staging"), CFG)
    assert f.rule_id == "ec2_idle" and f.action == ACTION_REMOVE and f.destructive
    assert f.estimated_monthly_savings == instance_monthly_cost("m5.2xlarge")
    assert f.alternatives and f.alternatives[0]["action"] == ACTION_RESIZE


def test_protection_tags_disable_every_rule():
    assert evaluate_resource(ec2(tags={"finops:ignore": "true"}), CFG) == []
    assert is_protected({"Retain": "yes"}) and not is_protected({"retain": "false"})


def test_orphan_volume_and_snapshot_rules():
    vol = NormalizedResource("aws", "storage", "ebs", "vol-1", "r", state="available", attached=False, size_gb=500,
                             volume_type="gp2", unattached_days=45, environment="production")
    (f,) = evaluate_resource(vol, CFG)
    assert f.action == ACTION_DELETE_VOLUME and f.estimated_monthly_savings == 50.0 and f.destructive
    vol.unattached_days = 3
    assert evaluate_resource(vol, CFG) == []

    snap = NormalizedResource("aws", "storage", "ebs_snapshot", "snap-1", "r", size_gb=300, age_days=200, environment="development")
    (g,) = evaluate_resource(snap, CFG)
    assert g.action == ACTION_DELETE_SNAPSHOT and g.estimated_monthly_savings == 15.0
    snap.attributes = {"ami_ids": ["ami-1"]}
    assert evaluate_resource(snap, CFG) == []
    snap.attributes = {"managed_by": "aws-backup"}
    assert evaluate_resource(snap, CFG) == []


def test_rule_config_overrides_ignore_invalid_values():
    cfg = RuleConfig.from_overrides({"orphan_volume_days": "30", "old_snapshot_days": "abc", "nonexistent": 1})
    assert cfg.orphan_volume_days == 30 and cfg.old_snapshot_days == 90


def test_environment_detection_is_conservative():
    assert environment_from_tags({"Environment": "PROD"}) == "production"
    assert environment_from_tags({"env": "stg"}) == "staging"
    assert environment_from_tags({}) == "unknown"
    assert risk.is_production_like("unknown") and not risk.is_production_like("development")


def test_risk_classification():
    (resize,) = evaluate_resource(ec2(environment="development", cpu_avg=3.0, cpu_max=20.0, memory_avg=8.0, monthly_cost=280.32), RuleConfig(idle_cpu_threshold=0.0))
    assert risk.classify_risk(resize) == risk.LOW           # no productivo + confianza alta
    (prod_resize,) = evaluate_resource(ec2(), CFG)
    assert risk.classify_risk(prod_resize) == risk.MEDIUM
    (idle_prod,) = evaluate_resource(ec2(cpu_avg=1.0, cpu_max=3.0, memory_avg=5.0), CFG)
    assert risk.classify_risk(idle_prod) == risk.HIGH
    snap = NormalizedResource("aws", "storage", "ebs_snapshot", "s", "r", size_gb=300, age_days=200, environment="production")
    (sf,) = evaluate_resource(snap, CFG)
    assert risk.classify_risk(sf) == risk.LOW
    assert risk.priority(390, 0.94, risk.LOW) == "P1" and risk.priority(10, 0.9, risk.HIGH) == "P3"
    assert risk.financial_impact(390) == "HIGH" and risk.financial_impact(60) == "MEDIUM" and risk.financial_impact(5) == "LOW"


def test_policy_standard_vs_reinforced():
    std = policy.evaluate(action=ACTION_RESIZE, risk="MEDIUM", environment="production", confidence=0.9)
    assert std.allowed and std.approvals_required == 1 and not std.reinforced and not std.pr_as_draft
    prod_del = policy.evaluate(action=ACTION_DELETE_VOLUME, risk="MEDIUM", environment="production", confidence=0.9)
    assert prod_del.reinforced and prod_del.approvals_required == 2 and prod_del.automation_blocked and prod_del.pr_as_draft
    unknown_del = policy.evaluate(action=ACTION_REMOVE, risk="MEDIUM", environment="unknown", confidence=0.9)
    assert unknown_del.reinforced                                   # desconocido = producción
    dev_del = policy.evaluate(action=ACTION_DELETE_SNAPSHOT, risk="LOW", environment="development", confidence=0.9)
    assert not dev_del.reinforced and dev_del.approvals_required == 1
    high = policy.evaluate(action=ACTION_RESIZE, risk="HIGH", environment="development", confidence=0.9)
    assert high.reinforced


def test_policy_blocks_unknown_actions_and_low_confidence():
    bad = policy.evaluate(action="DROP_DATABASE", risk="LOW", environment="development", confidence=0.99)
    assert not bad.allowed and "allowlist" in bad.blocked_reasons[0]
    low = policy.evaluate(action=ACTION_RESIZE, risk="LOW", environment="development", confidence=0.4)
    assert not low.allowed


def test_approval_satisfaction_rules():
    reinforced = policy.evaluate(action=ACTION_DELETE_VOLUME, risk="MEDIUM", environment="production", confidence=0.9)
    assert not policy.approval_satisfied(["FINOPS"], reinforced)
    assert not policy.approval_satisfied(["FINOPS", "FINOPS"], reinforced)       # falta ADMIN/SRE
    assert policy.approval_satisfied(["FINOPS", "SRE"], reinforced)
    std = policy.evaluate(action=ACTION_RESIZE, risk="MEDIUM", environment="production", confidence=0.9)
    assert policy.approval_satisfied(["FINOPS"], std)
    assert not policy.approval_satisfied(["VIEWER"], std) and not policy.can_approve("DEVELOPER")


def test_state_machine():
    path = [sm.DETECTED, sm.ANALYZED, sm.PROPOSED, sm.PENDING_APPROVAL, sm.APPROVED, sm.PR_CREATED, sm.MERGED, sm.DEPLOYED, sm.VERIFIED]
    for a, b in zip(path, path[1:]):
        sm.assert_transition(a, b)
    assert not sm.can_transition(sm.PENDING_APPROVAL, sm.PR_CREATED)       # no se salta la aprobación
    assert not sm.can_transition(sm.REJECTED, sm.APPROVED)
    assert sm.can_transition(sm.PR_CREATED, sm.APPROVED)
    try:
        sm.assert_transition(sm.MERGED, sm.APPROVED)
        raise AssertionError
    except sm.InvalidTransition:
        pass


def test_savings_realization_example_from_design_doc():
    r = savings.compute_realization(expected_monthly_savings=500, baseline_monthly_cost=800, observed_monthly_cost=368)
    assert r.observed_monthly_savings == 432 and r.realization_pct == 86.4
    assert savings.compute_realization(0, 10, 10).realization_pct is None
    assert savings.monthly_from_window(70.0, 14) == 152.19      # 70 / 14 días × 30,4375
