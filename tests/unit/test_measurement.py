"""Medición del ahorro observado: períodos alineados, control de uso, datos de modelo vs facturación y límites. Funciones puras."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from cloudcost.domain import measurement as ms
from cloudcost.domain.pricing import DAYS_PER_MONTH

DEPLOY = date(2026, 9, 1)
TODAY = date(2026, 9, 30)
PARAMS = {"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"}      # proporción de precios 0,5


def _windows(deploy=DEPLOY, today=TODAY):
    return ms.plan_windows(deploy, today)


def _series(w: ms.Windows, pre: float | list, post: float | list, *, source="cost_explorer"):
    """Coste diario del recurso en ambas ventanas; `post` puede ser una lista (con ceros para días apagados)."""
    daily, sources = {}, {}
    for i, d in enumerate(ms._days(w.pre_start, w.pre_end)):
        daily[d] = pre[i] if isinstance(pre, list) else pre
        sources[d] = source
    for i, d in enumerate(ms._days(w.post_start, w.post_end)):
        daily[d] = post[i] if isinstance(post, list) else post
        sources[d] = source
    return daily, sources


def _measure(w, daily, sources, **kw):
    base = dict(action="RESIZE_INSTANCE", params=PARAMS, windows=w, approved_savings=152.19, baseline_fallback_monthly=304.38,
                resource_daily=daily, sources=sources, billing_days=set(), resource_exists_now=True)
    base.update(kw)
    return ms.measure(**base)


# --------------------------------------------------------------------------- ventanas (control del período)
def test_windows_are_whole_weeks_with_transition_buffer_and_billing_lag():
    w = _windows()
    assert w.pre_days == 14 and w.pre_end == DEPLOY and w.pre_start == DEPLOY - timedelta(days=14)
    assert w.post_start == DEPLOY + timedelta(days=ms.BUFFER_DAYS)                    # el día del despliegue y el siguiente no cuentan
    assert w.post_days % 7 == 0 and 7 <= w.post_days <= 28
    assert w.post_end - timedelta(days=1) <= TODAY - timedelta(days=ms.DATA_LAG_DAYS)  # nunca los últimos días con retraso de facturación
    assert _windows(today=DEPLOY + timedelta(days=45)).post_days == 28                 # tope: no se arrastra ruido lejano


@pytest.mark.parametrize("days_after,ok", [(5, False), (9, False), (10, True), (17, True)])   # 10 = 2 de transición + 7 + 2 de retraso − 1
def test_not_enough_complete_days_after_deployment_is_refused_with_the_exact_count(days_after, ok):
    today = DEPLOY + timedelta(days=days_after)
    if ok:
        assert _windows(today=today).post_days >= 7
    else:
        with pytest.raises(ms.InsufficientData) as exc:
            _windows(today=today)
        assert "al menos 7 días" in str(exc.value)


def test_monthly_figure_does_not_depend_on_the_calendar_month_length():
    """28, 30 y 31 días dan el mismo coste mensual con el mismo coste diario: se promedia por día, no por mes natural."""
    for deploy in (date(2026, 2, 10), date(2026, 4, 10), date(2026, 7, 10)):
        w = ms.plan_windows(deploy, deploy + timedelta(days=30))
        daily, sources = _series(w, 10.0, 5.0)
        m = _measure(w, daily, sources)
        assert m.baseline_monthly_cost == round(10.0 * DAYS_PER_MONTH, 2) and m.observed_monthly_cost_adjusted == round(5.0 * DAYS_PER_MONTH, 2)


# --------------------------------------------------------------------------- reducción de tamaño
def test_resize_matching_the_price_ratio_is_confirmed_with_high_confidence():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    m = _measure(w, daily, sources)
    assert m.attribution == "confirmed" and m.confidence_grade == "high" and m.data_grade == "billing"
    assert m.adjusted_savings == pytest.approx(5.0 * DAYS_PER_MONTH, abs=0.01) and m.raw_savings == m.adjusted_savings
    assert m.realization_pct == pytest.approx(100.0, abs=0.1) and m.confounders == []
    assert m.controls["price_ratio"] == 0.5 and m.controls["deviation_from_expected"] == 0.0


def test_a_week_with_the_instance_stopped_is_not_counted_as_savings():
    """Control de uso: sin apagados la reducción ahorra 5/día; la instancia estuvo parada 4 días y la diferencia bruta se infla."""
    w = _windows()
    post = [5.0] * w.post_days
    for i in (3, 4, 5, 6):
        post[i] = 0.0
    daily, sources = _series(w, 10.0, post)
    m = _measure(w, daily, sources)
    assert m.raw_savings > m.adjusted_savings                                         # lo bruto atribuye al resize lo que fue parada
    assert m.adjusted_savings == pytest.approx(5.0 * DAYS_PER_MONTH, abs=0.01)         # el ajustado coincide con el caso limpio
    assert any(c.code == "active_days_changed" for c in m.confounders)
    assert m.controls["active_days_ratio"] == {"pre": 1.0, "post": round((w.post_days - 4) / w.post_days, 3)}
    assert m.confidence_grade == "medium"                                              # hay un factor medio: no es «alta»


def test_change_not_applied_is_detected():
    w = _windows()
    daily, sources = _series(w, 10.0, 9.8)
    m = _measure(w, daily, sources)
    assert m.attribution == "not_applied" and m.adjusted_savings < 0.05 * 10 * DAYS_PER_MONTH


def test_partial_saving_is_reported_as_partial():
    w = _windows()
    daily, sources = _series(w, 10.0, 7.0)                                             # esperaba 5, bajó a 7
    m = _measure(w, daily, sources)
    assert m.attribution == "partial" and 0 < m.adjusted_savings < 5.0 * DAYS_PER_MONTH


def test_saving_beyond_what_the_resize_explains_is_flagged_not_celebrated():
    w = _windows()
    daily, sources = _series(w, 10.0, 2.5)
    m = _measure(w, daily, sources)
    assert m.attribution == "exceeds_model" and any(c.code == "saving_exceeds_model" for c in m.confounders)
    assert m.realization_pct > 100


def test_service_wide_cost_shift_makes_the_result_confounded_and_gives_a_control_adjusted_figure():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    peers_pre, peers_post = [100.0] * 14, [145.0] * 14                                  # el resto del servicio subió 45 %
    m = _measure(w, daily, sources, peers_pre=peers_pre, peers_post=peers_post)
    assert m.attribution == "confounded" and m.confidence_grade == "low"
    assert any(c.code == "service_wide_change" and c.severity == "high" for c in m.confounders)
    assert m.controls["service_peers_trend"] == 0.45
    assert m.controls["control_adjusted_savings"] == pytest.approx(10.0 * DAYS_PER_MONTH * 1.45 - 5.0 * DAYS_PER_MONTH, abs=0.01)


def test_stable_peers_do_not_add_noise():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    m = _measure(w, daily, sources, peers_pre=[100.0] * 14, peers_post=[104.0] * 14)
    assert m.attribution == "confirmed" and m.controls["service_peers_trend"] == 0.04 and "control_adjusted_savings" not in m.controls


def test_cpu_far_from_the_expected_after_the_change_flags_a_workload_change():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    ok = _measure(w, daily, sources, usage_pre={"cpu_avg": 10.0}, usage_post={"cpu_avg": 19.0})        # esperada: 20 (la mitad de vCPU)
    assert not any(c.code == "usage_changed" for c in ok.confounders) and ok.controls["cpu"]["expected_post"] == 20.0
    grew = _measure(w, daily, sources, usage_pre={"cpu_avg": 10.0}, usage_post={"cpu_avg": 38.0})
    assert any(c.code == "usage_changed" and c.severity == "high" for c in grew.confounders) and grew.attribution == "confounded"


# --------------------------------------------------------------------------- datos: facturación vs modelo
def test_model_data_is_never_presented_as_an_observation():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0, source="estimate")                          # filas calculadas con la tabla de precios
    m = _measure(w, daily, sources)
    assert m.data_grade == "model" and m.attribution == "inconclusive" and m.confidence_grade == "low"
    assert any(c.code == "model_data" and c.severity == "high" for c in m.confounders)


def test_missing_pre_window_falls_back_to_the_stored_baseline_then_to_the_estimate():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    for d in ms._days(w.pre_start, w.pre_end):
        daily.pop(d), sources.pop(d)
    stored = _measure(w, daily, sources, baseline_monthly_stored=304.0, baseline_grade_stored="billing")
    assert stored.baseline_monthly_cost == 304.0 and stored.controls["baseline_source"] == "stored_baseline"
    assert any(c.code == "baseline_from_snapshot" for c in stored.confounders)
    est = _measure(w, daily, sources)
    assert est.controls["baseline_source"] == "approved_estimate" and est.data_grade == "model" and est.confidence_grade == "low"


def test_incomplete_post_data_is_flagged_high():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    for d in ms._days(w.post_start, w.post_end)[:9]:
        daily.pop(d), sources.pop(d)
    m = _measure(w, daily, sources)
    assert any(c.code == "incomplete_post_data" for c in m.confounders) and m.confidence_grade == "low"


def test_no_post_data_at_all_is_inconclusive():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    for d in ms._days(w.post_start, w.post_end):
        daily.pop(d), sources.pop(d)
    m = _measure(w, daily, sources)
    assert m.attribution == "inconclusive" and m.adjusted_savings == 0.0


# --------------------------------------------------------------------------- eliminaciones
def _delete(w, post_daily, *, exists, billing_days=None, pre=6.0):
    daily, sources = _series(w, pre, 0.0)
    for d in ms._days(w.post_start, w.post_end):
        daily.pop(d), sources.pop(d)
    for d, v in post_daily.items():
        daily[d], sources[d] = v, "cost_explorer"
    return ms.measure(action="DELETE_VOLUME", params={}, windows=w, approved_savings=pre * DAYS_PER_MONTH, baseline_fallback_monthly=0.0,
                      resource_daily=daily, sources=sources, billing_days=billing_days if billing_days is not None else set(),
                      resource_exists_now=exists)


def test_deleted_resource_counts_missing_days_as_zero_only_where_the_account_has_billing():
    w = _windows()
    days = set(ms._days(w.post_start, w.post_end))
    m = _delete(w, {}, exists=False, billing_days=days)
    assert m.attribution == "confirmed" and m.adjusted_savings == pytest.approx(6.0 * DAYS_PER_MONTH, abs=0.01)
    assert m.confidence_grade == "high" and m.data_grade == "billing"
    no_coverage = _delete(w, {}, exists=False, billing_days=set())                     # un hueco de datos NO es un cero
    assert no_coverage.attribution == "inconclusive" and no_coverage.adjusted_savings == 0.0


def test_resource_still_existing_and_billed_means_not_applied():
    w = _windows()
    still = {d: 6.0 for d in ms._days(w.post_start, w.post_end)}
    m = _delete(w, still, exists=True)
    assert m.attribution == "not_applied" and abs(m.adjusted_savings) < 0.01
    assert any(c.code == "resource_still_exists" for c in m.confounders)


def test_residual_cost_after_deletion_is_partial():
    w = _windows()
    residual = {d: 1.2 for d in ms._days(w.post_start, w.post_end)}                    # 20 % del anterior (snapshot final, etc.)
    m = _delete(w, residual, exists=False)
    assert m.attribution == "partial" and any(c.code == "residual_cost" for c in m.confounders)


def test_actions_without_billing_per_resource_cannot_be_measured():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    m = ms.measure(action="RIGHTSIZE_WORKLOAD", params={}, windows=w, approved_savings=50, baseline_fallback_monthly=100,
                   resource_daily=daily, sources=sources, billing_days=set(), resource_exists_now=True)
    assert m.attribution == "inconclusive" and m.confidence_grade == "low" and "manualmente" in " ".join(m.limitations)


def test_limitations_are_always_stated():
    w = _windows()
    daily, sources = _series(w, 10.0, 5.0)
    text = " ".join(_measure(w, daily, sources).limitations)
    assert "causalidad" in text and "Savings Plans" in text
