"""Herramienta de validación contra una cuenta de laboratorio: instantánea + informe determinista + anonimización.

Aquí la «cuenta» es la sesión con respuestas validadas por los modelos de la API (tests/aws_stub.py). Lo que se prueba es la herramienta:
que registra qué operaciones se invocan, que el informe es reproducible byte a byte desde la instantánea y que la anonimización no deja
identificadores. La validación contra una cuenta REAL la hace quien ejecute `python -m cloudcost.cli aws-lab` (docs/aws-lab-validation.md).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from aws_stub import ACCOUNT, TODAY, StubbedSession, describe_instances, instance
from cloudcost import cli, lab
from cloudcost.collectors.aws_guard import ALLOWED_OPERATIONS
from test_aws_collector_stubbed import _happy

CFG = lab.LabConfig(account_ref=ACCOUNT, regions=["us-east-1"], months=6, today=TODAY, sleep=lambda s: None)


@pytest.fixture()
def snap():
    return lab.collect_snapshot(CFG, session=_happy())


def test_snapshot_records_every_aws_operation_and_none_outside_the_read_only_list(snap):
    called = {(c["service"], c["operation"]) for c in snap["api_calls"]}
    assert called == set(ALLOWED_OPERATIONS)
    assert all(c["errors"] == 0 and c["iam_action"] != "?" for c in snap["api_calls"])
    assert next(c for c in snap["api_calls"] if c["operation"] == "GetCostAndUsage")["count"] == 2
    assert snap["meta"]["identity_verified"] is True and snap["meta"]["account_last4"] == "3333" and ACCOUNT not in json.dumps(snap["meta"])
    assert snap["findings"] and snap["findings"][0]["formula_id"].endswith(".v1") and snap["findings"][0]["reference"]["verified"] is True


def test_report_is_deterministic_and_reproducible_from_the_saved_snapshot(snap, tmp_path):
    first = lab.render_report(snap)
    assert first == lab.render_report(json.loads(json.dumps(snap)))                    # misma instantánea → mismo texto
    paths = lab.write_outputs(snap, str(tmp_path / "run1"))
    assert Path(paths["report"]).read_text(encoding="utf-8") == first
    digest_line = Path(paths["digest"]).read_text().strip()
    assert digest_line == f"{hashlib.sha256(first.encode()).hexdigest()}  report.md"
    # regenerar SIN acceso a la cuenta, desde el archivo, por la CLI
    assert cli.main(["aws-lab", "--from-snapshot", paths["snapshot"], "--out-dir", str(tmp_path / "run2")]) == 0
    assert (tmp_path / "run2" / "report.md").read_bytes() == (tmp_path / "run1" / "report.md").read_bytes()
    assert (tmp_path / "run2" / "report.sha256").read_text() == digest_line + "\n"


def test_report_content_covers_the_validation_criteria(snap):
    text = lab.render_report(snap)
    for needle in ("## 1. Resultado de la validación", "## 2. Operaciones de AWS invocadas", "Escrituras o modificaciones: **0**",
                   "## 4. Cobertura de Cost Explorer", "## 5. Coste de la cuenta por servicio y región", "ec2_other", "us-east-1",
                   "## 6. Hallazgos (ahorro ESTIMADO, no observado)", "## 7. Incidencias clasificadas", "## 8. Limitaciones", "ce:GetCostAndUsage",
                   "Huella de la instantánea", "sin incidencias"):
        assert needle in text, needle
    assert "Solicitudes a Cost Explorer: 4" in text


def test_report_lists_classified_issues_and_says_what_to_fix():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "x", "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/a/b"})
    s.fail("ec2", "DescribeInstances", "UnauthorizedOperation", "no", status=403)
    s.respond("ec2", "DescribeVolumes", {"Volumes": []})
    s.respond("ec2", "DescribeImages", {"Images": []})
    s.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    s.fail("ce", "GetCostAndUsageWithResources", "AccessDeniedException", "User not enabled for cost explorer access", status=403)
    s.fail("ce", "GetCostAndUsage", "AccessDeniedException", "User not enabled for cost explorer access", status=403)
    s.fail("ce", "GetCostAndUsage", "AccessDeniedException", "User not enabled for cost explorer access", status=403)
    snap = lab.collect_snapshot(CFG, session=s)
    text = lab.render_report(snap)
    assert snap["partial"] is True and "con incidencias" in text and "Inventario parcial" in text
    assert "permission_denied" in text and "not_enabled" in text and "Billing → Cost Explorer" in text
    assert [c["errors"] for c in snap["api_calls"] if c["operation"] == "DescribeInstances"] == [1]


def test_identity_mismatch_aborts_the_validation_and_says_so():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": "999988887777", "UserId": "x", "Arn": "arn:aws:sts::999988887777:assumed-role/a/b"})
    snap = lab.collect_snapshot(CFG, session=s)
    text = lab.render_report(snap)
    assert snap["meta"]["aborted"] and "INCOMPLETA" in text and "****7777" in text and "999988887777" not in json.dumps(snap)
    assert snap["resources"] == [] and snap["meta"]["identity_verified"] is False


def test_cli_rejects_an_invalid_account_and_missing_snapshot(tmp_path, capsys):
    assert cli.main(["aws-lab", "--account-ref", "mi-cuenta", "--out-dir", str(tmp_path)]) == 2
    assert "12 dígitos" in capsys.readouterr().err
    with pytest.raises(FileNotFoundError):
        cli.main(["aws-lab", "--from-snapshot", str(tmp_path / "no-existe.json"), "--out-dir", str(tmp_path)])


def test_cli_refuses_a_snapshot_of_an_unknown_version(tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text(json.dumps({"version": 99}))
    with pytest.raises(ValueError, match="no soportada"):
        cli.main(["aws-lab", "--from-snapshot", str(bad), "--out-dir", str(tmp_path / "o")])


# --------------------------------------------------------------------------- anonimización
def test_anonymized_snapshot_and_report_contain_no_identifiers():
    s = _happy()
    cfg = lab.LabConfig(account_ref=ACCOUNT, regions=["us-east-1"], anonymize=True, salt="sal-de-prueba", scale=0.5, today=TODAY, sleep=lambda s: None)
    snap = lab.collect_snapshot(cfg, session=s)
    blob = json.dumps(snap) + lab.render_report(snap)
    for raw in ("i-0aaa", "vol-0aaa", "vol-0orph", "snap-0old", ACCOUNT, "111122223333", '"web"', "Name"):
        assert raw not in blob, raw
    assert snap["meta"]["anonymized"] is True and snap["meta"]["scale"] == 0.5 and "importes ×0.5" in lab.render_report(snap)
    assert all(r["account_ref"] == "****3333" for r in snap["account_costs"])
    assert all(r["name"].startswith("name-") for r in snap["resources"] if r["name"])
    assert all(set(r["tags"]) <= {"environment", "Environment", "env", "stage"} for r in snap["resources"])


def test_pseudonyms_are_stable_per_salt_and_amounts_scale_linearly():
    plain = lab.collect_snapshot(CFG, session=_happy())
    a = lab.collect_snapshot(lab.LabConfig(account_ref=ACCOUNT, anonymize=True, salt="s1", scale=1.0, today=TODAY, sleep=lambda s: None), session=_happy())
    b = lab.collect_snapshot(lab.LabConfig(account_ref=ACCOUNT, anonymize=True, salt="s1", scale=2.0, today=TODAY, sleep=lambda s: None), session=_happy())
    c = lab.collect_snapshot(lab.LabConfig(account_ref=ACCOUNT, anonymize=True, salt="s2", scale=1.0, today=TODAY, sleep=lambda s: None), session=_happy())
    ids = lambda s: [r["resource_id"] for r in s["resources"]]  # noqa: E731
    assert ids(a) == ids(b) != ids(c)
    assert len(a["findings"]) == len(plain["findings"])
    assert b["findings"][0]["estimated_monthly_savings"] == pytest.approx(2 * a["findings"][0]["estimated_monthly_savings"], abs=0.02)
    assert a["findings"][0]["estimated_monthly_savings"] == pytest.approx(plain["findings"][0]["estimated_monthly_savings"], abs=0.01)
    ratio = lambda s: sum(f["estimated_monthly_savings"] for f in s["findings"]) / sum(f["current_monthly_cost"] for f in s["findings"])  # noqa: E731
    assert ratio(a) == pytest.approx(ratio(b), rel=0.01)                                                  # las proporciones se conservan


def test_a_run_with_no_cost_explorer_still_produces_a_valid_report():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "x", "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/a/b"})
    s.respond("ec2", "DescribeInstances", describe_instances(instance("i-0aaa")))
    s.respond("ec2", "DescribeVolumes", {"Volumes": []})
    s.respond("ec2", "DescribeImages", {"Images": []})
    s.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    s.respond("cloudwatch", "ListMetrics", {"Metrics": []})
    from aws_stub import metric_data

    s.respond("cloudwatch", "GetMetricData", metric_data(("c0a", [2.0] * 336), ("c0x", [8.0] * 336)))
    snap = lab.collect_snapshot(lab.LabConfig(account_ref=ACCOUNT, use_cost_explorer=False), session=s)
    text = lab.render_report(snap)
    assert "Cost Explorer: desactivado" in text and "Sin datos de coste de la cuenta" in text
    assert all(c["service"] != "ce" for c in snap["api_calls"]) and snap["findings"][0]["reference"]["verified"] is False
