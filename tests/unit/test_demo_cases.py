"""Casos de la demostración: RDS abandonados y volúmenes huérfanos de despliegues fallidos (y sus contrafuertes que NO deben salir)."""
from __future__ import annotations

from pathlib import Path

import pytest
from cloudcost.collectors.demo import DemoCollector
from cloudcost.domain import policy as pol
from cloudcost.domain import risk
from cloudcost.domain.pricing import rds_monthly_cost
from cloudcost.domain.rules import ACTION_DELETE_DB, RuleConfig, evaluate_all, rule_rds_idle
from cloudcost.iac.patcher import PatchError, build_patch
from cloudcost.iac.terraform import IacIndex

EXAMPLE = Path(__file__).resolve().parents[2] / "infrastructure/terraform/example-iac/main.tf"
CFG = RuleConfig()


@pytest.fixture(scope="module")
def world():
    resources = DemoCollector().collect().resources
    index = IacIndex.build({"main.tf": EXAMPLE.read_text(encoding="utf-8")})
    for r in resources:
        block = index.match(r)
        if block:
            r.iac_address = block.address
    findings = {f.resource.name: f for f in evaluate_all(resources, CFG)}
    return resources, index, findings


def test_totales_de_la_demo(world):
    resources, _, findings = world
    assert len(resources) == 21 and len(findings) == 13
    assert round(sum(f.estimated_monthly_savings for f in findings.values()), 2) == 1573.00


def test_tres_rds_abandonados_con_riesgo_alto_y_aprobacion_reforzada(world):
    _, _, findings = world
    rds = {n: f for n, f in findings.items() if f.rule_id == "rds_idle"}
    assert set(rds) == {"orders-legacy-dev", "reports-stg-old", "legacy-crm-prod"}
    for f in rds.values():
        assert f.action == ACTION_DELETE_DB and f.destructive and f.projected_monthly_cost == 0.0
        assert risk.classify_risk(f) == risk.HIGH
        decision = pol.evaluate(action=f.action, risk=risk.HIGH, environment=f.resource.environment, confidence=f.confidence)
        assert decision.allowed and decision.approvals_required == 2 and decision.automation_blocked and decision.pr_as_draft
        assert f.alternatives and f.evidence["connections_avg"] <= CFG.rds_idle_connections


def test_las_bases_que_no_deben_proponerse(world):
    _, _, findings = world
    assert "customers-prod" not in findings          # en uso real (85 conexiones)
    assert "audit-archive-hold" not in findings      # etiqueta legal-hold


def test_costo_de_rds_multi_az_y_proteccion_contra_borrado(world):
    _, _, findings = world
    assert rds_monthly_cost("db.m5.large", 200) == 147.83
    assert rds_monthly_cost("db.r5.large", 500, multi_az=True) == 465.4 and findings["reports-stg-old"].current_monthly_cost == 465.4
    crm = findings["legacy-crm-prod"]
    assert crm.confidence < findings["orders-legacy-dev"].confidence and "protección contra borrado" in crm.summary


def test_parches_de_rds(world):
    _, index, findings = world
    ok = findings["orders-legacy-dev"]
    patch = build_patch(action=ok.action, params=ok.params, block=index.block_by_address(ok.resource.iac_address), index=index)
    assert "-resource \"aws_db_instance\" \"orders_legacy\"" in patch.diff
    ref = findings["reports-stg-old"]
    with pytest.raises(PatchError) as err:                          # lo referencia un aws_ssm_parameter
        build_patch(action=ref.action, params=ref.params, block=index.block_by_address(ref.resource.iac_address), index=index)
    assert err.value.code == "referenced"


def test_volumenes_de_despliegues_fallidos(world):
    resources, index, findings = world
    failed = {n for n in findings if n.endswith("failed-deploy") or n.endswith("-failed")}
    assert failed == {"pvc-3f9a-failed-deploy", "pvc-8c21-failed-deploy", "tmp-rollout-aug-failed", "canary-sep-failed"}
    assert "deploy-oct-in-progress" not in findings        # 3 días sin attachment: aún puede ser un despliegue en curso
    assert "db-export-keep" not in findings                # finops:ignore
    assert findings["pvc-3f9a-failed-deploy"].resource.iac_address is None       # lo creó Kubernetes: sin bloque Terraform
    canary = findings["canary-sep-failed"]
    assert pol.evaluate(action=canary.action, risk=risk.classify_risk(canary), environment="production",
                        confidence=canary.confidence).approvals_required == 2
    tmp = findings["tmp-rollout-aug-failed"]
    build_patch(action=tmp.action, params=tmp.params, block=index.block_by_address(tmp.resource.iac_address), index=index)


def test_rds_idle_no_actua_sin_datos_o_ventana_corta(world):
    resources, _, _ = world
    db = next(r for r in resources if r.name == "orders-legacy-dev")
    assert rule_rds_idle(db, CFG) is not None
    db.attributes["connections_avg"] = None
    assert rule_rds_idle(db, CFG) is None
    db.attributes["connections_avg"] = 0.0
    db.observation_days = 5
    assert rule_rds_idle(db, CFG) is None
    db.observation_days = 45
    db.state = "stopped"
    assert rule_rds_idle(db, CFG) is None
