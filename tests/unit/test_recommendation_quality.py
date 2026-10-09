"""Calidad de las recomendaciones: fundamento de cada cifra, datos ausentes, costes atípicos, duplicados e incompatibles, huella de evidencia."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from cloudcost.domain.estimates import FORMULAS, price_table_check
from cloudcost.domain.models import NormalizedResource
from cloudcost.domain.rules import RuleConfig, evaluate_all, evaluate_resource
from cloudcost.services import evidence as ev

CFG = RuleConfig()
DOCS = Path(__file__).resolve().parents[2] / "docs"


def ec2(**kw):
    base = dict(provider="aws", resource_type="compute", service="ec2", resource_id="i-1", region="us-east-1", state="running",
                instance_type="m5.2xlarge", cpu_avg=12.4, cpu_max=30.0, memory_avg=19.8, observation_days=30, environment="production",
                monthly_cost=280.32, cost_source="cost_explorer", tags={},
                attributes={"cost_basis": {"source": "cost_explorer", "window_days": 14, "window_start": "2026-09-24", "window_end": "2026-10-08",
                                           "days_with_data": 14, "quality_flags": [], "as_of": "2026-10-08"}})
    base.update(kw)
    return NormalizedResource(**base)


def volume(**kw):
    base = dict(provider="aws", resource_type="storage", service="ebs", resource_id="vol-1", region="us-east-1", state="available",
                attached=False, volume_type="gp3", size_gb=500.0, unattached_days=40, environment="staging", monthly_cost=40.0,
                cost_source="cost_explorer", tags={}, attributes={})
    base.update(kw)
    return NormalizedResource(**base)


def snap(**kw):
    base = dict(provider="aws", resource_type="storage", service="ebs_snapshot", resource_id="snap-1", region="us-east-1", size_gb=400.0,
                age_days=300, environment="staging", monthly_cost=20.0, cost_source="estimate_upper_bound", tags={}, attributes={})
    base.update(kw)
    return NormalizedResource(**base)


def rds(**kw):
    base = dict(provider="aws", resource_type="database", service="rds", resource_id="db-1", region="us-east-1", state="available",
                instance_type="db.m5.large", size_gb=100.0, cpu_avg=1.0, observation_days=30, environment="development",
                monthly_cost=0.0, cost_source="estimate", tags={}, attributes={"connections_avg": 0.0, "connections_max": 0})
    base.update(kw)
    return NormalizedResource(**base)


# --------------------------------------------------------------------------- fundamento de cada cifra
CASES = {
    "ec2_downsize": lambda: ec2(),
    "ec2_idle": lambda: ec2(cpu_avg=1.0, cpu_max=4.0, memory_avg=6.0, environment="staging"),
    "ebs_orphan": lambda: volume(),
    "snapshot_old": lambda: snap(),
    "rds_idle": lambda: rds(),
}


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_every_finding_carries_formula_reference_cost_assumptions_and_limits(rule_id):
    (f,) = evaluate_resource(CASES[rule_id](), CFG)
    assert f.rule_id == rule_id
    est = f.evidence["estimate"]
    assert est["formula_id"] == f"{rule_id}.v1" == FORMULAS[rule_id].id
    assert est["expression"] == FORMULAS[rule_id].expression and est["kind"] == FORMULAS[rule_id].kind
    ref = est["reference"]
    assert ref["monthly_cost"] == pytest.approx(f.current_monthly_cost) and ref["source"] and ref["as_of"]
    assert est["assumptions"] and est["limitations"]
    assert any("USD por mes" in a for a in est["assumptions"])                       # supuestos comunes siempre presentes
    assert est["result"]["estimated_monthly_savings"] == f.estimated_monthly_savings
    assert est["result"]["annualized"] == pytest.approx(f.estimated_monthly_savings * 12, abs=0.01)


def test_k8s_finding_also_carries_its_formula():
    res = NormalizedResource(
        provider="kubernetes", resource_type="kubernetes", service="k8s_workload", resource_id="shop/checkout/app", region="cluster",
        name="shop/checkout/app", state="running", environment="production", observation_days=14,
        attributes={"namespace": "shop", "kind": "Deployment", "workload": "checkout", "container": "app", "replicas": 3,
                    "cpu_request": 1.0, "mem_request": 2 * 1024**3, "cpu_avg_cores": 0.1, "cpu_p95_cores": 0.25, "cpu_max_cores": 0.3,
                    "mem_avg_bytes": 500 * 1024**2, "mem_max_bytes": 800 * 1024**2})
    (f,) = evaluate_resource(res, CFG)
    assert f.evidence["estimate"]["formula_id"] == "k8s_overprovisioned.v1" and f.evidence["estimate"]["kind"] == "reserved"
    assert f.evidence["estimate"]["inputs"]["replicas"] == 3


def test_estimated_cost_basis_is_called_out_in_the_assumptions():
    est = evaluate_resource(rds(), CFG)[0].evidence["estimate"]
    assert est["reference"]["verified"] is False
    assert any("ESTIMADO" in a for a in est["assumptions"])
    real = evaluate_resource(ec2(), CFG)[0].evidence["estimate"]
    assert real["reference"]["verified"] is True and not any("ESTIMADO" in a for a in real["assumptions"])
    assert real["reference"]["window_start"] == "2026-09-24" and real["reference"]["days_with_data"] == 14


def test_resize_inputs_reproduce_the_saving_by_hand():
    (f,) = evaluate_resource(ec2(), CFG)
    i = f.evidence["estimate"]["inputs"]
    assert f.estimated_monthly_savings == round(i["reference_monthly_cost"] * (1 - i["price_ratio"]), 2)
    assert i["price_ratio"] == round(i["hourly_price_target"] / i["hourly_price_current"], 4)


def test_snapshot_saving_is_declared_an_upper_bound():
    est = evaluate_resource(snap(), CFG)[0].evidence["estimate"]
    assert est["kind"] == "upper_bound" and "≤" in est["expression"] and any("SUPERIOR" in x for x in est["limitations"])


def test_documented_formulas_match_the_code():
    """docs/savings-methodology.md es la versión humana de FORMULAS: ninguna fórmula puede cambiar sin cambiar el documento."""
    text = (DOCS / "savings-methodology.md").read_text(encoding="utf-8")
    for f in FORMULAS.values():
        assert f.id in text, f"{f.id} no está documentada"
        assert f.expression in text, f"la expresión de {f.id} difiere de la documentada"


# --------------------------------------------------------------------------- invariantes sobre una rejilla de casos
def _grid():
    for cost in (0.0, 4.99, 5.0, 50.0, 280.32, 5000.0):
        for cpu, mem, peak in ((0.5, 5.0, 2.0), (2.5, 20.0, 9.0), (12.0, 20.0, 40.0), (14.9, 29.9, 60.0), (30.0, 50.0, 90.0)):
            for env in ("production", "staging", "unknown"):
                yield ec2(monthly_cost=cost, cpu_avg=cpu, memory_avg=mem, cpu_max=peak, environment=env)
    for gb in (1.0, 50.0, 4000.0):
        for days in (13, 14, 200):
            yield volume(size_gb=gb, unattached_days=days, monthly_cost=0.0, cost_source="estimate")
            yield snap(size_gb=gb, age_days=days + 80, monthly_cost=0.0)


def test_invariants_hold_for_every_finding_on_a_grid():
    n = 0
    for res in _grid():
        for f in evaluate_resource(res, CFG):
            n += 1
            assert 0 <= f.estimated_monthly_savings <= f.current_monthly_cost + 0.01, (res, f)
            assert f.projected_monthly_cost >= -0.01 and f.projected_monthly_cost <= f.current_monthly_cost + 0.01
            assert f.estimated_monthly_savings >= CFG.min_monthly_savings
            assert 0.0 <= f.confidence <= 0.97
            assert all(math.isfinite(v) for v in (f.current_monthly_cost, f.projected_monthly_cost, f.estimated_monthly_savings))
            assert f.evidence["estimate"]["formula_id"]
    assert n > 40                                                                    # la rejilla realmente produce hallazgos


# --------------------------------------------------------------------------- datos ausentes
@pytest.mark.parametrize("override", [
    {"cpu_avg": None}, {"memory_avg": None}, {"observation_days": 0}, {"observation_days": 13}, {"state": None}, {"state": "stopped"},
    {"instance_type": None, "monthly_cost": 0.0}, {"instance_type": "zz.unknown", "monthly_cost": 0.0}, {"monthly_cost": 0.0, "instance_type": None},
])
def test_missing_or_insufficient_data_never_produces_a_recommendation(override):
    assert evaluate_resource(ec2(**override), CFG) == []


def test_unknown_instance_type_with_real_cost_cannot_be_resized_because_there_is_nothing_to_compare_with():
    assert [f.rule_id for f in evaluate_resource(ec2(instance_type="zz.unknown"), CFG)] == []


@pytest.mark.parametrize("override", [
    {"unattached_days": None}, {"unattached_days": 13}, {"attached": None}, {"attached": True}, {"state": "in-use"}, {"size_gb": None, "monthly_cost": 0.0},
    {"monthly_cost": 0.0, "size_gb": 10.0, "volume_type": "gp3"},
])
def test_volume_rule_requires_complete_evidence(override):
    assert evaluate_resource(volume(**override), CFG) == []


def test_snapshot_without_age_or_protected_or_in_use_by_an_image_is_never_proposed():
    assert evaluate_resource(snap(age_days=None), CFG) == []
    assert evaluate_resource(snap(attributes={"ami_ids": ["ami-1"]}), CFG) == []
    assert evaluate_resource(snap(attributes={"managed_by": "aws-backup"}), CFG) == []
    assert evaluate_resource(snap(tags={"keep": "true"}), CFG) == []


def test_rds_without_connection_metrics_is_not_assumed_idle():
    assert evaluate_resource(rds(attributes={}), CFG) == []
    assert evaluate_resource(rds(cpu_avg=None), CFG) == []


def test_protected_resources_are_never_touched():
    assert evaluate_resource(ec2(tags={"do-not-delete": "true"}), CFG) == []
    assert evaluate_resource(volume(tags={"legal-hold": "yes"}), CFG) == []


# --------------------------------------------------------------------------- costes atípicos
def test_cost_far_above_the_price_table_lowers_confidence_and_says_why():
    base = evaluate_resource(ec2(), CFG)[0]
    odd = evaluate_resource(ec2(monthly_cost=280.32 * 4), CFG)[0]                     # 4× la tabla: licencias, transferencia, pico...
    assert odd.confidence == pytest.approx(base.confidence - 0.10, abs=0.002)
    assert odd.evidence["confidence_adjustments"][0]["reason"].startswith("cost_outlier_vs_price_table")
    assert any("tabla de precios" in a and "4.0×" in a for a in odd.evidence["estimate"]["assumptions"])
    assert odd.evidence["estimate"]["reference"]["vs_price_table"]["outlier"] is True


def test_cost_far_below_the_price_table_is_also_flagged():
    odd = evaluate_resource(ec2(monthly_cost=60.0), CFG)[0]
    assert odd.evidence["estimate"]["reference"]["vs_price_table"]["outlier"] is True and odd.evidence["confidence_adjustments"]


def test_typical_cost_has_no_adjustments():
    f = evaluate_resource(ec2(monthly_cost=300.0), CFG)[0]
    assert "confidence_adjustments" not in f.evidence and f.evidence["estimate"]["reference"]["vs_price_table"]["outlier"] is False


def test_estimated_costs_are_not_compared_with_the_table_they_came_from():
    assert price_table_check(ec2(cost_source="estimate"), 280.32) is None


def test_series_quality_flags_reduce_confidence_once_and_are_explained():
    clean = evaluate_resource(ec2(), CFG)[0]
    flagged = ec2(attributes={"cost_basis": {"source": "cost_explorer", "quality_flags": ["spike", "trend_break"], "as_of": "2026-10-08"}})
    f = evaluate_resource(flagged, CFG)[0]
    assert f.confidence == pytest.approx(clean.confidence - 0.10, abs=0.002)           # una sola vez aunque haya dos señales
    assert f.evidence["confidence_adjustments"][0]["reason"] == "cost_quality:spike,trend_break"
    assert any("señales de calidad" in a for a in f.evidence["estimate"]["assumptions"])


def test_credits_in_the_series_make_the_cost_unverified():
    credited = ec2(attributes={"cost_basis": {"source": "cost_explorer", "quality_flags": ["negative_amount"], "as_of": "2026-10-08"}})
    f = evaluate_resource(credited, CFG)[0]
    assert f.evidence["cost_basis"]["verified"] is False                               # se propone, pero marcado como no verificado
    assert evaluate_resource(credited, RuleConfig(require_verified_cost=True)) == []   # y con la política estricta no se propone


def test_extreme_costs_do_not_break_the_arithmetic():
    big = evaluate_resource(ec2(monthly_cost=10_000_000.0), CFG)[0]
    assert big.estimated_monthly_savings == pytest.approx(5_000_000.0, rel=0.01) and math.isfinite(big.confidence)
    assert evaluate_resource(ec2(monthly_cost=0.01), CFG) == []                        # bajo el mínimo de ahorro


# --------------------------------------------------------------------------- duplicadas e incompatibles
def test_resize_identity_does_not_depend_on_the_target_type():
    """Si las métricas cambian y el destino pasa de m5.xlarge a m5.large, es la MISMA recomendación (no dos incompatibles)."""
    a = evaluate_resource(ec2(), CFG)[0]
    b = evaluate_resource(ec2(memory_avg=8.0, cpu_avg=5.0, cpu_max=15.0), CFG)[0]
    assert a.params["target_instance_type"] != b.params["target_instance_type"]
    assert a.dedupe_key("acc") == b.dedupe_key("acc")
    assert a.dedupe_key("acc") != a.dedupe_key("other-account")
    other = evaluate_resource(ec2(resource_id="i-2"), CFG)[0]
    assert a.dedupe_key("acc") != other.dedupe_key("acc")


def test_idle_supersedes_resize_and_keeps_it_as_an_alternative():
    findings = evaluate_resource(ec2(cpu_avg=1.0, cpu_max=4.0, memory_avg=6.0), CFG)
    assert [f.rule_id for f in findings] == ["ec2_idle"]
    assert findings[0].alternatives and findings[0].alternatives[0]["action"] == "RESIZE_INSTANCE"


def test_no_two_findings_share_a_dedupe_key_or_target_the_same_resource_with_incompatible_actions():
    resources = list(_grid())
    findings = evaluate_all(resources, CFG)
    keys = [f.dedupe_key("acc") for f in findings]
    assert len(keys) == len(set(keys)) or len({(f.resource.resource_id, f.rule_id) for f in findings}) < len(findings)
    by_resource: dict[tuple, set[str]] = {}
    for f in findings:
        by_resource.setdefault((id(f.resource), ), set()).add(f.action)
    for actions in by_resource.values():
        assert not ({"REMOVE_RESOURCE", "RESIZE_INSTANCE"} <= actions)               # nunca «borrar» y «reducir» el mismo recurso


# --------------------------------------------------------------------------- huella de la evidencia
def test_hash_ignores_volatile_timestamps_but_not_content():
    base = {"rule_id": "r", "formula_id": "r.v1", "params": {"a": 1}, "estimated_monthly_savings": 10.0, "reference_cost": 20.0,
            "confidence": 0.9, "evidence": {"estimate": {"reference": {"as_of": "2026-10-08", "monthly_cost": 20.0}}, "x": [1, 2]}, "assumptions": ["a"]}
    h = ev.evidence_hash(base)
    later = json.loads(json.dumps(base))
    later["evidence"]["estimate"]["reference"]["as_of"] = "2026-10-09"
    assert ev.evidence_hash(later) == h
    changed = json.loads(json.dumps(base))
    changed["evidence"]["x"] = [1, 3]
    assert ev.evidence_hash(changed) != h
    reordered = {k: base[k] for k in reversed(list(base))}
    assert ev.evidence_hash(reordered) == h


def test_hash_is_stable_across_numeric_representations():
    assert ev.canonical_json({"a": 10.0}) == ev.canonical_json({"a": 10}) == '{"a":10}'
    assert ev.canonical_json({"a": 0.1 + 0.2}) == '{"a":0.3}'
    assert ev.canonical_json({"a": float("nan")}) == '{"a":null}'


def test_material_change_thresholds():
    prev = {"estimated_monthly_savings": 100.0, "confidence": 0.8, "reference_cost_source": "cost_explorer", "inputs": {"params": {"t": "a"}}}
    kw = dict(estimated=100.0, confidence=0.8, source="cost_explorer", params={"t": "a"})
    assert ev.material(None, **kw) is True
    assert ev.material(prev, **kw) is False
    assert ev.material(prev, **{**kw, "estimated": 100.9}) is False                  # céntimos de oscilación diaria: no es material
    assert ev.material(prev, **{**kw, "estimated": 102.0}) is True
    assert ev.material(prev, **{**kw, "confidence": 0.86}) is True
    assert ev.material(prev, **{**kw, "source": "estimate"}) is True
    assert ev.material(prev, **{**kw, "params": {"t": "b"}}) is True
