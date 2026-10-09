"""Evaluación OPA/Rego dentro de la API: plan sintético, motor real (si hay `opa`), fallo cerrado y aislamiento del subproceso.

Las pruebas que necesitan el binario `opa` se omiten si no está en el PATH (el job `api` de CI lo instala con setup-opa).
"""
from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest
from cloudcost import guardrail
from cloudcost.config import Settings
from cloudcost.domain.rules import ACTION_DELETE_VOLUME, ACTION_REMOVE, ACTION_RESIZE
from cloudcost.guardrail import GuardrailVerdict, approval_context, evaluate_plan, plan_from_change

POLICIES = Path(__file__).resolve().parents[2] / "packages" / "policies"
needs_opa = pytest.mark.skipif(shutil.which("opa") is None, reason="binario opa no disponible")


def _settings(**kw) -> Settings:
    return Settings(opa_policy_dir=str(POLICIES), **kw)


def _resource(env="production", **kw):
    return {"resource_id": "i-1", "environment": env, "tags": {"Name": "web", "env": "x"}, "instance_type": "m5.2xlarge",
            "volume_type": "gp3", "size_gb": 100.0, **kw}


def _rec(**kw):
    return {"id": "rec-1", "risk": "HIGH", "approvals_required": 2, "policy": {"reinforced": True}, **kw}


def _approvals(*pairs):
    return [{"approver_role": r, "reason": reason} for r, reason in pairs]


def _plan(action=ACTION_REMOVE, env="production", approvals=None, tf_type="aws_instance", rec=None, params=None):
    return plan_from_change(tf_type=tf_type, address=f"{tf_type}.x", action=action, params=params or {}, resource=_resource(env),
                            rec=rec or _rec(), approvals=approvals or [])


# --------------------------------------------------------------------------- plan sintético y contexto
def test_plan_from_change_shapes_for_resize_and_delete():
    resize = _plan(ACTION_RESIZE, params={"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"})
    change = resize["resource_changes"][0]
    assert change["address"] == "aws_instance.x" and change["change"]["actions"] == ["update"]
    assert change["change"]["before"]["instance_type"] == "m5.2xlarge" and change["change"]["after"]["instance_type"] == "m5.xlarge"
    assert resize["cloudcost"]["environments"] == {"aws_instance.x": "production"}
    delete = _plan(ACTION_DELETE_VOLUME, tf_type="aws_ebs_volume")["resource_changes"][0]["change"]
    assert delete["actions"] == ["delete"] and delete["after"] is None and delete["before"]["size"] == 100.0


def test_approval_context_requires_reinforced_roles_and_count():
    both = approval_context(_rec(), _approvals(("FINOPS", "ok"), ("SRE", "ok")))
    assert both["reinforced_approval"] is True and both["approvals"] == 2
    assert approval_context(_rec(), _approvals(("FINOPS", "a"), ("FINOPS", "b")))["reinforced_approval"] is False   # sin ADMIN/SRE
    assert approval_context(_rec(), _approvals(("SRE", "a")))["reinforced_approval"] is False                       # faltan aprobadores
    assert approval_context(_rec(policy={"reinforced": False}), _approvals(("SRE", "a"), ("ADMIN", "b")))["reinforced_approval"] is False


def test_approval_context_extracts_change_ticket_from_reason():
    assert approval_context(_rec(), _approvals(("SRE", "autorizado según CHG-1234 por el CAB")))["change_ticket"] == "CHG-1234"
    assert approval_context(_rec(), _approvals(("SRE", "visto bueno")))["change_ticket"] is None
    assert approval_context(_rec(), _approvals(("SRE", "chg-1234 en minúsculas no cuenta")))["change_ticket"] is None


# --------------------------------------------------------------------------- motor real
@needs_opa
def test_prod_delete_is_denied_without_reinforced_approval_and_allowed_with_it():
    denied = evaluate_plan(_settings(), _plan(), mode="enforce")
    assert denied.evaluated and denied.blocked and denied.result == "denied"
    assert len(denied.violations) == 1 and "aws_instance.x" in denied.violations[0] and denied.policy_digest
    ok = evaluate_plan(_settings(), _plan(approvals=_approvals(("ADMIN", "ok"), ("SRE", "ok"))), mode="enforce")
    assert ok.evaluated and not ok.blocked and ok.result == "allowed" and ok.violations == []


@needs_opa
def test_dev_delete_and_unknown_environment():
    assert evaluate_plan(_settings(), _plan(env="development"), mode="enforce").violations == []
    assert len(evaluate_plan(_settings(), _plan(env="unknown"), mode="enforce").violations) == 1      # desconocido = producción


@needs_opa
def test_prod_resize_needs_ticket_cited_in_approval():
    params = {"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"}
    rec = _rec(risk="MEDIUM", approvals_required=1, policy={"reinforced": False})
    assert len(evaluate_plan(_settings(), _plan(ACTION_RESIZE, rec=rec, params=params), mode="enforce").violations) == 1
    with_ticket = _plan(ACTION_RESIZE, rec=rec, params=params, approvals=_approvals(("FINOPS", "Aprobado, ticket CHG-77")))
    assert evaluate_plan(_settings(), with_ticket, mode="enforce").violations == []


@needs_opa
def test_custom_extra_policy_directory_is_loaded(tmp_path):
    (tmp_path / "custom.rego").write_text(
        'package cloudcost.guardrail\nimport rego.v1\n'
        'deny contains "regla propia del cliente" if input.cloudcost.risk == "HIGH"\n')
    (tmp_path / "custom_test.rego").write_text("package x\nthis is not valid rego")            # los *_test.rego se ignoran
    verdict = evaluate_plan(_settings(opa_extra_policy_dir=str(tmp_path)), _plan(env="development"), mode="enforce")
    assert verdict.violations == ["regla propia del cliente"]


@needs_opa
def test_invalid_policy_is_an_engine_error_not_a_silent_allow(tmp_path):
    (tmp_path / "bad.rego").write_text("package cloudcost.guardrail\nthis is not rego {{{")
    verdict = evaluate_plan(_settings(opa_extra_policy_dir=str(tmp_path)), _plan(), mode="enforce")
    assert verdict.error and not verdict.evaluated and verdict.result == "error" and verdict.blocked    # fail-closed


def test_policy_digest_changes_with_content(tmp_path):
    (tmp_path / "a.rego").write_text("package a\n")
    first = guardrail.digest_of(sorted(tmp_path.glob("*.rego")))
    assert first == guardrail.digest_of(sorted(tmp_path.glob("*.rego")))
    (tmp_path / "a.rego").write_text("package a\n# otro\n")
    assert first != guardrail.digest_of(sorted(tmp_path.glob("*.rego")))


# --------------------------------------------------------------------------- modos y fallo cerrado (binario simulado)
def _fake_opa(tmp_path, body: str) -> str:
    path = tmp_path / "fake-opa"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_missing_binary_fails_closed_in_enforce_and_open_in_audit():
    enforce = evaluate_plan(_settings(opa_binary="no-existe-opa"), _plan(), mode="enforce")
    assert enforce.error and "no encontrado" in enforce.error and enforce.blocked
    audit_mode = evaluate_plan(_settings(opa_binary="no-existe-opa"), _plan(), mode="audit")
    assert audit_mode.error and not audit_mode.blocked                         # solo se registra


def test_off_mode_does_not_run_anything():
    verdict = evaluate_plan(_settings(opa_binary="no-existe-opa"), _plan(), mode="off")
    assert not verdict.evaluated and verdict.error is None and not verdict.blocked and verdict.policy_digest is None


def test_timeout_is_an_error(tmp_path):
    binary = _fake_opa(tmp_path, "import time\ntime.sleep(10)")
    verdict = evaluate_plan(_settings(opa_binary=binary, opa_timeout_seconds=0.3), _plan(), mode="enforce")
    assert verdict.error and "tiempo límite" in verdict.error and verdict.blocked


def test_nonzero_exit_and_garbage_output_are_errors(tmp_path):
    failing = _fake_opa(tmp_path, 'print(json.dumps({"errors": [{"message": "rego_parse_error: boom"}]}))\nsys.exit(2)')
    v = evaluate_plan(_settings(opa_binary=failing), _plan(), mode="enforce")
    assert v.error and "boom" in v.error and v.blocked
    garbage = _fake_opa(tmp_path, 'print("<html>no soy json</html>")')
    assert "ilegible" in (evaluate_plan(_settings(opa_binary=garbage), _plan(), mode="enforce").error or "")


def test_subprocess_gets_a_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secreto")
    monkeypatch.setenv("AUTH_PEPPER", "otro-secreto")
    binary = _fake_opa(tmp_path, 'leaked = sorted(k for k in os.environ if k in ("AWS_SECRET_ACCESS_KEY", "AUTH_PEPPER"))\n'
                                 'print(json.dumps({"result": [{"expressions": [{"value": leaked}]}]}))')
    verdict = evaluate_plan(_settings(opa_binary=binary), _plan(), mode="audit")
    assert verdict.evaluated and verdict.violations == []                       # el subproceso no vio ningún secreto del proceso


def test_oversized_plan_is_rejected_before_running(tmp_path):
    binary = _fake_opa(tmp_path, 'print(json.dumps({"result": []}))')
    plan = _plan()
    plan["padding"] = "x" * (guardrail.MAX_PLAN_BYTES + 10)
    assert "demasiado grande" in (evaluate_plan(_settings(opa_binary=binary), plan, mode="enforce").error or "")


def test_result_without_violations_is_allowed(tmp_path):
    binary = _fake_opa(tmp_path, 'print("{}")')                                  # `opa eval` imprime {} si la consulta no devuelve nada
    verdict = evaluate_plan(_settings(opa_binary=binary), _plan(), mode="enforce")
    assert verdict.evaluated and verdict.violations == [] and not verdict.blocked


def test_effective_mode_defaults_by_environment():
    assert Settings(env="development").effective_opa_mode == "audit"
    assert Settings(opa_mode="enforce").effective_opa_mode == "enforce"
    assert Settings(opa_mode="off", env="development").effective_opa_mode == "off"


def test_verdict_blocking_semantics():
    assert GuardrailVerdict("enforce", violations=["x"]).blocked
    assert GuardrailVerdict("enforce", error="boom").blocked
    assert not GuardrailVerdict("audit", violations=["x"]).blocked and not GuardrailVerdict("enforce", evaluated=True).blocked
    payload = GuardrailVerdict("audit", violations=["x"], policy_digest="abc").audit_payload()
    assert payload["result"] == "denied" and json.dumps(payload)
