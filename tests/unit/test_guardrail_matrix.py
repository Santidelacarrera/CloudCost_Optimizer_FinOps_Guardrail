"""Políticas OPA/Rego: matriz de casos PERMITIDOS y DENEGADOS con el motor real, para todos los tipos de recurso que CloudCost puede tocar.

Hallazgo que motivó esta matriz: el guardrail solo cubría `aws_instance`, `aws_ebs_volume` y `aws_ebs_snapshot`. Eliminar una base de datos RDS
de producción —la acción más delicada— y cualquier eliminación o cambio de tamaño en Azure o GCP pasaban sin violación incluso en modo `enforce`.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from cloudcost.config import Settings
from cloudcost.domain.models import TF_TYPES
from cloudcost.domain.rules import ACTION_DELETE_DB, ACTION_DELETE_SNAPSHOT, ACTION_DELETE_VOLUME, ACTION_REMOVE, ACTION_RESIZE
from cloudcost.guardrail import evaluate_plan, plan_from_change

POLICIES = Path(__file__).resolve().parents[2] / "packages" / "policies"
needs_opa = pytest.mark.skipif(shutil.which("opa") is None, reason="binario opa no disponible")
SETTINGS = Settings(opa_policy_dir=str(POLICIES))

DELETE_ACTION = {"instance": ACTION_REMOVE, "volume": ACTION_DELETE_VOLUME, "snapshot": ACTION_DELETE_SNAPSHOT, "database": ACTION_DELETE_DB}


def _kind(tf_type: str) -> str:
    if tf_type == "aws_db_instance":
        return "database"
    if "snapshot" in tf_type:
        return "snapshot"
    if "disk" in tf_type or tf_type == "aws_ebs_volume":
        return "volume"
    return "instance"


ALL_TYPES = sorted({t for types in TF_TYPES.values() for t in types})
SIZABLE = ["aws_instance", "azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine", "google_compute_instance"]
PARAMS = {"aws_instance": ("m5.2xlarge", "m5.xlarge"), "azurerm_linux_virtual_machine": ("Standard_D8s_v5", "Standard_D4s_v5"),
          "azurerm_windows_virtual_machine": ("Standard_D8s_v5", "Standard_D4s_v5"), "google_compute_instance": ("n2-standard-8", "n2-standard-4")}


def _rec(reinforced=True):
    return {"id": "rec-1", "risk": "HIGH", "approvals_required": 2, "policy": {"reinforced": reinforced}}


def _plan(tf_type, action, env, approvals=(), reinforced=True, tags=None):
    params = {}
    if action == ACTION_RESIZE:
        params = {"current_instance_type": PARAMS[tf_type][0], "target_instance_type": PARAMS[tf_type][1]}
    resource = {"resource_id": "x", "environment": env, "tags": tags if tags is not None else {"Name": "x"}, "instance_type": params.get("current_instance_type"),
                "volume_type": "gp3", "size_gb": 100.0}
    return plan_from_change(tf_type=tf_type, address=f"{tf_type}.x", action=action, params=params, resource=resource, rec=_rec(reinforced),
                            approvals=[{"approver_role": r, "reason": why} for r, why in approvals])


def _verdict(plan):
    v = evaluate_plan(SETTINGS, plan, mode="enforce")
    assert v.evaluated and v.error is None, v.error
    return v


BOTH = (("FINOPS", "ok"), ("SRE", "ok"))
TWO_FINOPS = (("FINOPS", "ok"), ("FINOPS", "ok"))


def test_every_resource_type_cloudcost_can_patch_is_covered_by_the_rego_policy():
    """Si se añade un tipo a TF_TYPES sin añadirlo a `destructive_types`, ese tipo quedaría sin guardrail: la prueba falla."""
    rego = (POLICIES / "guardrail.rego").read_text(encoding="utf-8")
    block = rego[rego.index("destructive_types := {"):rego.index("size_attribute")]
    declared = set(re.findall(r'"([a-z0-9_]+)"', block))
    assert set(ALL_TYPES) <= declared, f"tipos sin guardrail: {sorted(set(ALL_TYPES) - declared)}"
    sized = set(re.findall(r'"([a-z_]+)": "(?:instance_type|size|machine_type)"', rego))
    assert set(SIZABLE) == sized


# --------------------------------------------------------------------------- DENEGADOS
@needs_opa
@pytest.mark.parametrize("tf_type", ALL_TYPES)
@pytest.mark.parametrize("env", ["production", "unknown"])
def test_deleting_any_production_or_unclassified_resource_is_denied_without_reinforced_approval(tf_type, env):
    action = DELETE_ACTION[_kind(tf_type)]
    v = _verdict(_plan(tf_type, action, env))
    assert v.blocked and len(v.violations) == 1 and f"{tf_type}.x" in v.violations[0], (tf_type, env, v.violations)


@needs_opa
@pytest.mark.parametrize("tf_type", ALL_TYPES)
def test_two_approvers_without_an_admin_or_sre_do_not_unlock_a_production_delete(tf_type):
    v = _verdict(_plan(tf_type, DELETE_ACTION[_kind(tf_type)], "production", approvals=TWO_FINOPS))
    assert v.blocked, "dos FINOPS no son aprobación reforzada"


@needs_opa
def test_reinforced_flag_without_the_approvals_does_not_unlock():
    """`policy.reinforced` solo marca que HACÍA falta; lo que cuenta son las aprobaciones reales."""
    assert _verdict(_plan("aws_db_instance", ACTION_DELETE_DB, "production", approvals=(("SRE", "ok"),))).blocked
    assert _verdict(_plan("aws_db_instance", ACTION_DELETE_DB, "production", approvals=())).blocked


@needs_opa
@pytest.mark.parametrize("tf_type", SIZABLE)
def test_resizing_in_production_needs_a_cited_change_ticket(tf_type):
    v = _verdict(_plan(tf_type, ACTION_RESIZE, "production", approvals=(("FINOPS", "lo vi y está bien"),), reinforced=False))
    assert v.blocked and "tamaño" in v.violations[0]


@needs_opa
@pytest.mark.parametrize("ticket_text", ["chg-1234 en minúsculas", "CHG1234 sin guion", "ticket pendiente", "", "CHG-", "-1234"])
def test_malformed_ticket_references_do_not_authorize_a_resize(ticket_text):
    assert _verdict(_plan("aws_instance", ACTION_RESIZE, "production", approvals=(("FINOPS", ticket_text),), reinforced=False)).blocked


@needs_opa
def test_an_empty_tag_value_does_not_count_as_authorization():
    v = _verdict(_plan("aws_instance", ACTION_REMOVE, "production", tags={"finops:reinforced-approval": ""}))
    assert v.blocked


@needs_opa
def test_every_violation_of_a_multi_change_plan_is_reported():
    plan = _plan("aws_instance", ACTION_REMOVE, "production")
    other = _plan("aws_db_instance", ACTION_DELETE_DB, "production")
    plan["resource_changes"] += other["resource_changes"]
    v = _verdict(plan)
    assert len(v.violations) == 2


# --------------------------------------------------------------------------- PERMITIDOS
@needs_opa
@pytest.mark.parametrize("tf_type", ALL_TYPES)
@pytest.mark.parametrize("env", ["development", "staging", "test"])
def test_deleting_non_production_resources_is_allowed(tf_type, env):
    v = _verdict(_plan(tf_type, DELETE_ACTION[_kind(tf_type)], env))
    assert not v.blocked and v.violations == []


@needs_opa
@pytest.mark.parametrize("tf_type", ALL_TYPES)
def test_reinforced_approval_by_a_finops_and_an_sre_allows_the_production_delete(tf_type):
    v = _verdict(_plan(tf_type, DELETE_ACTION[_kind(tf_type)], "production", approvals=BOTH))
    assert not v.blocked and v.violations == []


@needs_opa
@pytest.mark.parametrize("tf_type", SIZABLE)
def test_resize_with_a_ticket_in_the_approval_reason_is_allowed(tf_type):
    v = _verdict(_plan(tf_type, ACTION_RESIZE, "production", approvals=(("FINOPS", "Aprobado según CHG-4521"),), reinforced=False))
    assert not v.blocked


@needs_opa
@pytest.mark.parametrize("tf_type", SIZABLE)
@pytest.mark.parametrize("env", ["development", "staging"])
def test_resizing_outside_production_needs_no_ticket(tf_type, env):
    assert not _verdict(_plan(tf_type, ACTION_RESIZE, env, reinforced=False)).blocked


@needs_opa
def test_kubernetes_values_changes_are_not_destructive_and_pass():
    from cloudcost.domain.rules import ACTION_RIGHTSIZE_WORKLOAD

    plan = plan_from_change(tf_type="helm_values", address="charts/x/values.yaml#resources", action=ACTION_RIGHTSIZE_WORKLOAD,
                            params={}, resource={"resource_id": "x", "environment": "production", "tags": {}}, rec=_rec(), approvals=[])
    assert not _verdict(plan).blocked


@needs_opa
def test_resource_types_outside_the_list_are_ignored_by_design():
    plan = _plan("aws_instance", ACTION_REMOVE, "production")
    plan["resource_changes"][0]["type"] = "aws_s3_bucket"
    assert not _verdict(plan).blocked


# --------------------------------------------------------------------------- el contexto de la plataforma no se puede falsificar desde fuera
@needs_opa
def test_a_caller_supplied_cloudcost_context_is_only_trusted_when_the_platform_builds_it():
    """`plan_from_change` (plataforma) sí lo fija; el endpoint público lo descarta antes de evaluar (ver test_security_controls_api.py)."""
    plan = _plan("aws_db_instance", ACTION_DELETE_DB, "production")
    assert _verdict(plan).blocked
    plan["cloudcost"]["reinforced_approval"] = True
    assert not _verdict(plan).blocked                                        # confía en el contexto: por eso el endpoint lo elimina
