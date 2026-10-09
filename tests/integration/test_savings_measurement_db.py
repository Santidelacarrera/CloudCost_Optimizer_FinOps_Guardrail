"""Medición del ahorro observado de punta a punta contra PostgreSQL real: línea base, ventanas, controles y separación estimado/aprobado/observado.

El reloj se simula con fechas relativas a hoy: el despliegue ocurrió hace 20 días, así que hay 14 días previos y 14 posteriores completos.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from cloudcost.collectors.base import AccountCostRecord
from cloudcost.services import dashboard, workflow
from cloudcost.services.recommendations import WorkflowError
from dbkit import admin, daily_costs, instance, new_tenant, result

TODAY = date.today()
DEPLOY = TODAY - timedelta(days=20)
START = DEPLOY - timedelta(days=16)                    # historial desde 2 días antes de la ventana previa
DAYS = 36                                              # hasta hace 2 días


def _history(rid="i-0aaa", *, pre=10.0, post=5.0, source="cost_explorer", stopped=(), service="ec2"):
    def amount(d: date) -> float:
        if d in stopped:
            return 0.0
        return pre if d < DEPLOY else post
    return daily_costs(rid, service, START, DAYS, amount, source=source)


def _approve(t, rid="i-0aaa", rule=None):
    rec = t.rec(rid, rule)
    with t.tx() as conn:
        assert workflow.decide(conn, t.principal("FINOPS", "ana"), rec["id"], "APPROVED", "ok", rec["version"])["status"] == "APPROVED"
    # el tiempo pasó: se aprobó poco antes del despliegue y el PR se fusionó
    admin("update recommendations set status = 'MERGED', approved_at = %s where id = %s",
          (f"{DEPLOY - timedelta(days=2)} 10:00+00", str(rec["id"])))
    return rec


def _deploy(t, rec, on=DEPLOY):
    with t.tx() as conn:
        return workflow.mark_deployed(conn, t.principal("SRE", "bob"), rec["id"], reference="https://ci.example/run/7", deployed_on=on)


def _verify(t, rec, observed=None):
    with t.tx() as conn:
        return workflow.verify_savings(conn, t.principal("FINOPS", "ana"), rec["id"], observed_monthly_cost=observed)


def _row(t, rec):
    with t.tx() as conn:
        return conn.execute("select * from savings_verifications where recommendation_id = %s", (str(rec["id"]),)).fetchone()


@pytest.fixture()
def t():
    return new_tenant("save")


def _resize_flow(t, *, post=5.0, source="cost_explorer", stopped=(), account_costs=None, post_cpu=24.8):
    t.scan(result(instance(env="development", source=source), costs=_history(post=post, source=source, stopped=stopped)))
    rec = _approve(t)
    _deploy(t, rec)
    # escaneo posterior: la instancia ya es m5.xlarge y la CPU subió a ~el doble (la mitad de vCPU)
    t.scan(result(instance(env="development", itype="m5.xlarge", cost=140.16, cpu=post_cpu, mem=39.6, source=source),
                  costs=_history(post=post, source=source, stopped=stopped), account_costs=account_costs or []))
    return rec


# --------------------------------------------------------------------------- línea base
def test_baseline_is_recorded_at_approval_and_again_at_deployment_with_the_billed_window(t):
    t.scan(result(instance(env="development"), costs=_history()))
    rec = _approve(t)
    out = _deploy(t, rec)
    assert out["status"] == "DEPLOYED" and out["deployed_on"] == DEPLOY.isoformat()
    with t.tx() as conn:
        rows = conn.execute("select * from savings_baselines where recommendation_id = %s order by created_at", (str(rec["id"]),)).fetchall()
    assert [r["phase"] for r in rows] == ["approval", "deployment"]
    appr, dep = rows
    assert dep["window_start"] == DEPLOY - timedelta(days=14) and dep["window_end"] == DEPLOY
    assert dep["days_with_data"] == 14 and dep["data_grade"] == "billing" and dep["cost_source"] == "cost_explorer"
    assert float(dep["monthly_cost"]) == pytest.approx(10.0 * 30.4375, abs=0.01) and len(dep["daily_costs"]) == 14
    assert dep["usage"]["cpu_avg"] == 12.4 and dep["usage"]["instance_type"] == "m5.2xlarge"                   # utilización de ese momento
    assert appr["baseline_hash"] != dep["baseline_hash"]
    assert [e["payload"]["phase"] for e in t.audit("BASELINE_CAPTURED", rec["id"])] == ["approval", "deployment"]


def test_deployment_date_cannot_be_in_the_future_or_before_the_approval(t):
    t.scan(result(instance(env="development"), costs=_history()))
    rec = _approve(t)
    for bad in (TODAY + timedelta(days=1), DEPLOY - timedelta(days=30)):
        with pytest.raises(WorkflowError) as exc:
            _deploy(t, rec, bad)
        assert exc.value.code == "invalid_date"
    assert t.rec("i-0aaa")["status"] == "MERGED"


# --------------------------------------------------------------------------- medición controlada
def test_resize_is_measured_against_billing_with_aligned_windows_and_stored_with_its_controls(t):
    rec = _resize_flow(t)
    out = _verify(t, rec)
    assert out["status"] == "VERIFIED" and out["method"] == "cost_records_controlled"
    assert out["attribution"] == "confirmed" and out["confidence_grade"] == "high" and out["data_grade"] == "billing"
    assert out["baseline_monthly_cost"] == pytest.approx(304.38, abs=0.01) and out["observed_monthly_cost"] == pytest.approx(152.19, abs=0.01)
    assert out["observed_monthly_savings"] == pytest.approx(152.19, abs=0.01) and out["expected_monthly_savings"] == 140.16
    assert out["realization_pct"] == pytest.approx(108.6, abs=0.1) and out["estimated_monthly_savings"] == 140.16
    row = _row(t, rec)
    assert row["attribution"] == "confirmed" and row["data_grade"] == "billing" and row["baseline_id"] is not None
    assert (row["baseline_window_end"], row["window_start"]) == (DEPLOY, DEPLOY + timedelta(days=2))             # sin el día de transición
    assert (row["window_end"] - row["window_start"]).days == 14                                                    # semanas completas
    assert float(row["approved_monthly_savings"]) == 140.16 and float(row["raw_observed_monthly_savings"]) == pytest.approx(152.19, abs=0.01)
    assert row["controls"]["deviation_from_expected"] == 0.0 and row["controls"]["cpu"]["drift"] == 0.0
    assert row["limitations"] and "causalidad" in " ".join(row["limitations"])
    (ev,) = t.audit("SAVINGS_VERIFIED", rec["id"])
    assert ev["payload"]["attribution"] == "confirmed" and ev["payload"]["data_grade"] == "billing"


def test_an_instance_that_was_stopped_for_days_does_not_inflate_the_observed_saving(t):
    stopped = {DEPLOY + timedelta(days=5 + i) for i in range(4)}
    rec = _resize_flow(t, stopped=stopped)
    out = _verify(t, rec)
    row = _row(t, rec)
    assert float(row["raw_observed_monthly_savings"]) > out["observed_monthly_savings"] + 20                       # lo bruto se infla con la parada
    assert out["observed_monthly_savings"] == pytest.approx(152.19, abs=0.01)                                      # lo ajustado, no
    assert "active_days_changed" in out["confounders"] and out["confidence_grade"] == "medium"


def test_change_that_never_reached_production_is_reported_as_not_applied(t):
    rec = _resize_flow(t, post=9.9)
    out = _verify(t, rec)
    assert out["attribution"] == "not_applied" and out["realization_pct"] is not None and out["realization_pct"] < 5


def test_service_wide_cost_movement_is_reported_as_a_confounder(t):
    rows = []
    for i in range(DAYS):
        d = START + timedelta(days=i)
        total = 100.0 if d < DEPLOY else 160.0                                                                    # el servicio entero subió 60 %
        rows.append(AccountCostRecord("aws", "111122223333", "ec2", "Amazon Elastic Compute Cloud - Compute", "us-east-1", "DAILY",
                                      d, d + timedelta(days=1), total))
    rec = _resize_flow(t, account_costs=rows)
    out = _verify(t, rec)
    assert out["attribution"] == "confounded" and "service_wide_change" in out["confounders"] and out["confidence_grade"] == "low"
    assert _row(t, rec)["controls"]["service_peers_trend"] > 0.5
    assert "control_adjusted_savings" in _row(t, rec)["controls"]


def test_costs_from_the_price_table_are_not_an_observation(t):
    rec = _resize_flow(t, source="estimate")
    out = _verify(t, rec)
    assert out["data_grade"] == "model" and out["attribution"] == "inconclusive" and out["confidence_grade"] == "low"
    with t.tx() as conn:
        b = dashboard.breakdown(conn)["observed"]
    assert b["attributed"] == 0.0 and b["declared"] == pytest.approx(out["observed_monthly_savings"])               # cuenta como «declarado», no como observado


def test_too_few_days_after_the_change_is_refused_with_a_clear_reason(t):
    t.scan(result(instance(env="development"), costs=_history()))
    rec = _approve(t)
    admin("update recommendations set approved_at = %s where id = %s", (f"{TODAY - timedelta(days=9)} 10:00+00", str(rec["id"])))
    _deploy(t, rec, TODAY - timedelta(days=6))
    with pytest.raises(WorkflowError) as exc:
        _verify(t, rec)
    assert exc.value.status == 422 and exc.value.code == "insufficient_data" and "al menos 7 días" in exc.value.message
    assert t.rec("i-0aaa")["status"] == "DEPLOYED"                                                                 # no se verifica a medias


def test_manual_figure_is_stored_as_declared_never_as_measured(t):
    t.scan(result(instance(env="development"), costs=_history()))
    rec = _approve(t)
    _deploy(t, rec)
    out = _verify(t, rec, observed=160.0)
    assert out["method"] == "manual" and out["data_grade"] == "declared" and out["attribution"] == "unverified" and out["confidence_grade"] == "low"
    assert out["baseline_monthly_cost"] == pytest.approx(304.38, abs=0.01)                                          # línea base facturada, no la estimación
    with t.tx() as conn:
        s = dashboard.summary(conn)
    assert s["savings_breakdown"]["observed"]["attributed"] == 0.0 and s["savings_breakdown"]["observed"]["declared"] > 0
    assert s["realized_savings"] > 0                                                                                # el total histórico se conserva por compatibilidad


# --------------------------------------------------------------------------- eliminaciones
def _volume(rid="vol-0orph"):
    from cloudcost.domain.models import NormalizedResource
    return NormalizedResource("aws", "storage", "ebs", rid, "us-east-1", name=rid, environment="staging", volume_type="gp3", state="available",
                              attached=False, size_gb=500.0, unattached_days=40, monthly_cost=40.0, cost_source="cost_explorer", age_days=300,
                              tags={}, attributes={"cost_basis": {"source": "cost_explorer", "quality_flags": [], "as_of": "2026-10-08"}})


def _delete_flow(t, *, still_there: bool):
    pre = daily_costs("vol-0orph", "ebs", START, 16, 1.5)
    other = _history("i-0bbb", pre=9.0, post=9.0)                                      # otro recurso que sigue facturando: prueba de cobertura
    keep = [] if not still_there else daily_costs("vol-0orph", "ebs", DEPLOY, 20, 1.5)
    t.scan(result(_volume(), instance("i-0bbb", env="development", cpu=50, mem=60, peak=90), costs=pre + other + keep))
    rec = _approve(t, "vol-0orph")
    _deploy(t, rec)
    after = [instance("i-0bbb", env="development", cpu=50, mem=60, peak=90)] + ([_volume()] if still_there else [])
    t.scan(result(*after, costs=other + keep))
    return rec


def test_deleted_volume_is_confirmed_when_the_resource_is_gone_and_the_account_keeps_being_billed(t):
    rec = _delete_flow(t, still_there=False)
    out = _verify(t, rec)
    assert out["attribution"] == "confirmed" and out["data_grade"] == "billing"
    assert out["observed_monthly_savings"] == pytest.approx(1.5 * 30.4375, abs=0.01) and out["observed_monthly_cost"] == 0.0
    assert out["confidence_grade"] == "high"


def test_volume_still_present_and_billed_is_not_applied(t):
    rec = _delete_flow(t, still_there=True)
    out = _verify(t, rec)
    assert out["attribution"] == "not_applied" and abs(out["observed_monthly_savings"]) < 0.01
    assert "resource_still_exists" in out["confounders"]


# --------------------------------------------------------------------------- informe y panel
def test_dashboard_and_report_data_keep_estimated_approved_and_observed_apart(t):
    t.scan(result(instance(env="development"), instance("i-0ccc", env="development"), instance("i-0ddd", env="development", cpu=50.0, mem=60.0, peak=95.0),
                  costs=_history() + _history("i-0ccc")))
    rec = _approve(t)
    _deploy(t, rec)
    with t.tx() as conn:
        b = dashboard.breakdown(conn)
    assert b["estimated"]["count"] == 1 and b["estimated"]["monthly"] == 140.16                                    # i-0ccc, sin decidir
    assert b["approved"]["count"] == 1 and b["approved"]["monthly"] == 140.16                                       # i-0aaa, aprobada y desplegada
    assert b["observed"]["count"] == 0 and b["observed"]["attributed"] == 0.0

    t.scan(result(instance(env="development", itype="m5.xlarge", cost=140.16, cpu=24.8, mem=39.6), instance("i-0ccc", env="development"),
                  costs=_history() + _history("i-0ccc")))
    out = _verify(t, rec)
    with t.tx() as conn:
        b = dashboard.breakdown(conn)
        from cloudcost.reports import data as report_data
        data = report_data.build(conn, t.org)
    assert b["approved"]["count"] == 0 and b["observed"]["attributed"] == pytest.approx(out["observed_monthly_savings"])
    assert b["observed"]["realization_pct_attributed"] == pytest.approx(108.6, abs=0.1)
    (v,) = data["verified"]
    assert v["attribution_label"] == "Coherente con el cambio" and v["grade_label"] == "Alta" and v["raw_observed_monthly_savings"] > 0
    assert data["breakdown"]["estimated"]["monthly"] == 140.16


def test_executive_report_files_show_the_three_figures_and_the_limitations(t):
    from io import BytesIO

    from cloudcost.reports import data as report_data
    from cloudcost.reports.render import render_pdf, render_xlsx
    from openpyxl import load_workbook

    rec = _resize_flow(t)
    _verify(t, rec)
    with t.tx() as conn:
        data = report_data.build(conn, t.org)
    wb = load_workbook(BytesIO(render_xlsx(data)))
    text = " ".join(str(c.value) for row in wb["Resumen"].iter_rows() for c in row if c.value)
    assert "Estimado · aprobado · observado" in text and "no prueba causalidad" in text and "Realización (solo observado atribuible)" in text
    sheet = wb["Ahorro verificado"]
    assert [c.value for c in sheet[1]][:10] == ["Recomendación", "Aprobado/mes (USD)", "Observado ajustado/mes (USD)", "Realización", "Método",
                                                "Ventana", "Lectura", "Confianza", "Diferencia bruta/mes (USD)", "Otros factores"]
    assert sheet["E2"].value == "Medido (controlado)" and sheet["G2"].value == "Coherente con el cambio" and sheet["H2"].value == "Alta"
    pdf = render_pdf(data)
    assert pdf.startswith(b"%PDF") and len(pdf) > 3000
