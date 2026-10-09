"""Proyección de ahorro: modelo puro, sin base de datos."""
from __future__ import annotations

from datetime import date

import pytest
from cloudcost.services import projection

TODAY = date(2026, 10, 8)


def rec(status, savings, confidence=0.8):
    return {"status": status, "savings": savings, "confidence": confidence}


def months(out):
    return [p["month"] for p in out["points"]]


def test_add_months_cruza_anios():
    assert projection.add_months(date(2026, 10, 31), 3) == date(2027, 1, 1)
    assert projection.add_months(date(2026, 1, 15), -1) == date(2025, 12, 1)
    assert projection.add_months(date(2026, 3, 1), 0) == date(2026, 3, 1)


def test_sin_recomendaciones_optimizado_igual_a_actual():
    out = projection.project(today=TODAY, baseline=1000, recs=[], history={})
    future = [p for p in out["points"] if p["current"] is not None]
    assert all(p["current"] == p["optimized"] == p["expected"] == 1000 for p in future)
    assert out["cumulative_savings"] == {"optimized": 0.0, "expected": 0.0} and out["potential_monthly"] == 0.0


def test_meses_en_orden_y_mes_en_curso_es_la_linea_base():
    out = projection.project(today=TODAY, baseline=500, recs=[], history={}, months_back=3, months_ahead=4)
    assert months(out) == ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02"]
    now = next(p for p in out["points"] if p["month"] == "2026-10")
    assert now["actual"] == now["current"] == now["optimized"] == 500


def test_el_ahorro_empieza_segun_el_estado():
    recs = [rec("PR_CREATED", 100, 1.0), rec("PENDING_APPROVAL", 50, 1.0), rec("DEPLOYED", 10, 1.0)]
    out = projection.project(today=TODAY, baseline=1000, recs=recs, history={}, months_ahead=3)
    by = {p["month"]: p for p in out["points"]}
    assert by["2026-11"]["optimized"] == 890          # PR_CREATED + DEPLOYED desde el mes 1
    assert by["2026-12"]["optimized"] == 840          # + PENDING_APPROVAL desde el mes 2
    assert by["2027-01"]["optimized"] == 840
    assert out["cumulative_savings"]["optimized"] == 110 + 160 + 160
    assert out["potential_monthly"] == 160


def test_esperado_pondera_por_confianza_y_nunca_supera_al_optimizado():
    recs = [rec("APPROVED", 200, 0.5), rec("MERGED", 100, 0.9)]
    out = projection.project(today=TODAY, baseline=1000, recs=recs, history={}, months_ahead=2)
    nov = next(p for p in out["points"] if p["month"] == "2026-11")
    assert nov["optimized"] == 700 and nov["expected"] == 810
    assert out["expected_monthly"] == 190
    assert all(p["expected"] >= p["optimized"] for p in out["points"] if p["optimized"] is not None)


@pytest.mark.parametrize("status", ["REJECTED", "VERIFIED", "DETECTED", "ANALYZED", "PROPOSED"])
def test_estados_que_no_suman(status):
    out = projection.project(today=TODAY, baseline=1000, recs=[rec(status, 300)], history={})
    assert out["potential_monthly"] == 0.0


def test_el_optimizado_no_baja_de_cero():
    out = projection.project(today=TODAY, baseline=100, recs=[rec("APPROVED", 500, 1.0)], history={}, months_ahead=2)
    assert min(p["optimized"] for p in out["points"] if p["optimized"] is not None) == 0.0


def test_historial_con_huecos():
    out = projection.project(today=TODAY, baseline=900, recs=[], history={"2026-07": 800.456, "2026-09": 950}, months_back=3)
    hist = {p["month"]: p["actual"] for p in out["points"] if p["current"] is None and p["actual"] is None or p["month"] < "2026-10"}
    assert hist == {"2026-07": 800.46, "2026-08": None, "2026-09": 950}
    assert out["history_months_with_data"] == 2


def test_limites_de_horizonte():
    out = projection.project(today=TODAY, baseline=1, recs=[], history={}, months_back=99, months_ahead=99)
    assert len(out["points"]) == projection.MAX_BACK + 1 + projection.MAX_AHEAD
    assert len(projection.project(today=TODAY, baseline=1, recs=[], history={}, months_back=-5, months_ahead=0)["points"]) == 1 + 1 + 0


def test_ahorros_cero_o_negativos_se_ignoran():
    out = projection.project(today=TODAY, baseline=100, recs=[rec("APPROVED", 0), rec("APPROVED", -5)], history={})
    assert out["potential_monthly"] == 0.0


def test_los_supuestos_se_declaran():
    assert len(projection.project(today=TODAY, baseline=1, recs=[], history={})["assumptions"]) >= 3
