"""El escenario demo cuenta una historia reconocible: migración incompleta, despliegue fallido y restos de Kubernetes."""
from pathlib import Path

import pytest
from cloudcost.collectors.demo import DemoCollector
from cloudcost.domain import policy, risk
from cloudcost.domain.rules import RuleConfig, evaluate_all
from cloudcost.iac.patcher import PatchError, build_patch
from cloudcost.iac.terraform import IacIndex

EXAMPLE = Path(__file__).resolve().parents[2] / "infrastructure/terraform/example-iac/main.tf"


@pytest.fixture(scope="module")
def world():
    resources = DemoCollector().collect().resources
    idx = IacIndex.build({"main.tf": EXAMPLE.read_text()})
    for r in resources:
        block = idx.match(r)
        if block:
            r.iac_address = block.address
    findings = evaluate_all(resources, RuleConfig())
    return resources, idx, {f.resource.name: f for f in findings}


def test_totals_match_the_documented_numbers(world):
    resources, _, by_name = world
    assert len(resources) == 20 and len(by_name) == 15
    assert round(sum(f.estimated_monthly_savings for f in by_name.values()), 2) == 2383.75


def test_original_scenario_is_preserved(world):
    _, _, by_name = world
    original = ["web-prod-1", "batch-dev-1", "reports-stg-1", "legacy-data-prod", "scratch-dev", "old-backup-dev"]
    assert round(sum(by_name[n].estimated_monthly_savings for n in original), 2) == 777.42


def test_abandoned_aurora_cluster_is_found_with_both_members(world):
    _, _, by_name = world
    writer, reader = by_name["orders-legacy-writer"], by_name["orders-legacy-reader"]
    assert writer.evidence["cluster_id"] == reader.evidence["cluster_id"] == "orders-legacy"
    assert writer.estimated_monthly_savings == 787.5 and reader.estimated_monthly_savings == 422.5
    assert risk.classify_risk(writer) == "HIGH"
    d = policy.evaluate(action=writer.action, risk="HIGH", environment="production", confidence=writer.confidence)
    assert d.reinforced and d.approvals_required == 2 and d.automation_blocked


def test_busy_database_is_left_alone(world):
    _, _, by_name = world
    assert "billing-prod" not in by_name                         # 140 conexiones y CPU 34%: en uso


def test_database_without_final_snapshot_cannot_be_removed_by_patch(world):
    _, idx, by_name = world
    ok = by_name["orders-legacy-writer"]
    patch = build_patch(action=ok.action, params=ok.params, block=idx.block_by_address(ok.resource.iac_address), index=idx)
    assert "aws_db_instance" in patch.diff and patch.summary.endswith("bloque eliminado")
    no_snap = by_name["reports-pg-dev"]
    with pytest.raises(PatchError) as e:
        build_patch(action=no_snap.action, params=no_snap.params, block=idx.block_by_address(no_snap.resource.iac_address), index=idx)
    assert e.value.code == "no_final_snapshot"


def test_failed_rollout_leaves_four_volumes_that_point_to_the_stack(world):
    _, idx, by_name = world
    rollout = [f for n, f in by_name.items() if n.startswith("checkout-v2-rollout-data-")]
    assert len(rollout) == 4
    assert {f.evidence["origin"] for f in rollout} == {"cloudformation"}
    assert all(f.evidence["origin_ref"] == "checkout-v2-rollout" and "CloudFormation" in f.summary for f in rollout)
    assert sum(f.estimated_monthly_savings for f in rollout) == 200.0
    assert all(f.resource.iac_address for f in rollout)          # están en Terraform: el PR se puede generar


def test_kubernetes_leftovers_have_no_iac_and_say_where_to_clean(world):
    _, _, by_name = world
    pvc = [f for n, f in by_name.items() if n.startswith("pvc-")]
    assert len(pvc) == 2 and all(f.evidence["origin"] == "kubernetes_pvc" for f in pvc)
    assert all(f.resource.iac_address is None for f in pvc)
    assert "kubectl" in pvc[0].summary


def test_rds_rule_needs_the_connection_metric():
    r = next(x for x in DemoCollector().collect().resources if x.name == "orders-legacy-writer")
    r.attributes.pop("connections_max")
    assert [f for f in evaluate_all([r], RuleConfig()) if f.rule_id == "rds_idle"] == []
    r.attributes["connections_max"] = 0
    r.tags["finops:ignore"] = "true"
    assert evaluate_all([r], RuleConfig()) == []                  # etiqueta de protección
