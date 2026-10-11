"""Informe de optimización: determinismo, contenido honesto, comparación con presupuesto y CLI sin secretos en la salida."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from cloudcost import cli
from cloudcost.domain.budgets import BudgetPolicy
from cloudcost.reports.optimization import build_report, to_markdown

from tests.unit.test_datastore_rules import collect, info

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _report(**kw):
    result, _ = collect({"docs": info(2_000_000), "small": info(1000, 128)})
    return build_report(result.resources, generated_at=NOW, tenant="Acme", sources=["qdrant"], **kw)


def test_report_is_deterministic_and_sorted():
    a, b = _report(), _report()
    assert to_markdown(a) == to_markdown(b) and json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    sav = [f["estimated_monthly_savings"] for f in a["findings"]]
    assert sav == sorted(sav, reverse=True) and a["total_estimated_monthly_savings"] == round(sum(sav), 2) > 0


def test_markdown_is_honest_about_scope_and_verification():
    md = to_markdown(_report())
    assert "estimación, no ahorro observado" in md and "simuladas" in md and "consultivas" in md
    assert "Acme" in md and any(f["action"] == "QUANTIZE_VECTOR_COLLECTION" for f in _report()["findings"])


def test_budget_section_reflects_policy():
    rep = _report(budget=BudgetPolicy("api_usage", 100, mode="block"), spent_usd=150)
    assert rep["budget"]["level"] == "exceeded" and "## Presupuesto" in to_markdown(rep)
    assert "budget" not in _report()


def test_empty_inventory_gives_empty_report():
    rep = build_report([], generated_at=NOW, sources=[])
    assert rep["findings"] == [] and "Sin hallazgos" in to_markdown(rep)


def test_cli_requires_a_source(capsys):
    assert cli.main(["usage-report", "--out-dir", "/tmp/x-unused"]) == 2


def test_cli_rejects_non_secret_env_names_without_leaking(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:supersecretpw@h/db")
    code = cli.main(["usage-report", "--pg-dsn-env", "DATABASE_URL", "--out-dir", str(tmp_path)])
    out = capsys.readouterr()
    assert code == 1 and "supersecretpw" not in out.out + out.err
