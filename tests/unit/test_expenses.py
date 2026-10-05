"""Análisis de gastos: lector (formatos chileno/inglés, informe con secciones, tabular) y reglas. Datos ficticios."""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloudcost.expenses.analyzer import analyze, clp, pct
from cloudcost.expenses.parser import ExpenseFormatError, detect_period, parse_amount, parse_statements

# Réplica anonimizada del formato de un estado de gastos comunes (separador ;, secciones, subtotales, total y fondo).
REPORT = """Comunidad Edificio Ejemplo;;;;
Calle Falsa 123;;;;
RUT;11.111.111-1;GASTOS COMUNES MES DE AGOSTO   2025;;
Gastos Operacionales efectuados en ;;;ago-25;
;;Descripcion;Monto ($);
REMUNERACIONES;;;;
;;Ana Perez;$1.000.000;
;;Luis Soto;$500.000;
;;Pedro Rojas;$270.000;
;;Sub-Total;$1.770.000;
MANTENCION Y REPARACIONES;;;;
;;Ascensores;$280.000;
;;Mantencion jardines   Pedro Rojas;$150.000;
;;Aseo;$100.000;
;;Sub-Total;$530.000;
GASTOS DE CONSUMO;;;;
;;Electricidad;$800.000;
;;Agua ;$30.000;
;;Agua ;$4.000;
;;Telefono;$30.000;
;;Sub-Total;$864.000;
Total Gastos ;;;$3.164.000;
Fondo comun de reserva ;;;$200.000;
TOTAL GASTOS COMUNES;;;$3.364.000;
;;;;
"""


def test_parse_amount_formats():
    cases = {"$1.016.000": 1016000, "1.016.000": 1016000, "$ 3.920": 3920, "1,234.56": Decimal("1234.56"),
             "1.234,56": Decimal("1234.56"), "(1.234)": -1234, "-3.920": -3920, "0,5": Decimal("0.5"), "$0": 0, "12": 12}
    for raw, want in cases.items():
        assert parse_amount(raw) == Decimal(str(want)), raw
    for raw in ("", "RUT", "53.331.931-9", "2025-08", "abc", None):
        assert parse_amount(raw) is None, raw


def test_detect_period():
    assert detect_period("GASTOS COMUNES MES DE AGOSTO   2025") == (2025, 8)
    assert detect_period("ago-25") == (2025, 8)
    assert detect_period("2025-03") == (2025, 3) and detect_period("03/2025") == (2025, 3)
    assert detect_period("15/08/2025") == (2025, 8)
    assert detect_period("Marco Lavarrelo") is None and detect_period("septiembre 2024") == (2024, 9)


def test_report_layout_parsed_and_totals_not_counted_as_spending():
    (st,) = parse_statements(REPORT, "x.csv")
    assert st.period_key == "2025-08"
    assert len(st.items) == 11 and {i.section for i in st.items} >= {"REMUNERACIONES", "GASTOS DE CONSUMO", "Otros"}
    assert sum(i.amount for i in st.items) == Decimal(3364000)          # subtotales/totales no se suman
    assert [t.kind for t in st.totals] == ["subtotal"] * 3 + ["total"] * 2
    assert not any("Sub-Total" in i.label or "TOTAL" in i.label.upper() for i in st.items)


def test_report_reconciles_and_flags_expected_leads():
    r = analyze(parse_statements(REPORT, "x.csv"))
    assert r["checks"] == {"passed": 5, "failed": 0}
    rules = {f["rule"]: f for f in r["findings"]}
    assert "RECONCILIATION" not in rules
    assert rules["REPEATED_ITEM"]["amount"] == "34000"                 # Agua ×2 con montos distintos → verificar, no alertar
    assert rules["SAME_PAYEE_TWO_SECTIONS"]["amount"] == "420000"      # sueldo + «mantencion jardines» de la misma persona
    assert rules["CONCENTRATION"]["title"].startswith("«REMUNERACIONES» concentra 52,6 %")
    assert rules["DOMINANT_ITEM"]["title"].startswith("«Electricidad»")
    assert all(f["severity"] in ("alert", "review", "info") for f in r["findings"])
    sev = [f["severity"] for f in r["findings"]]
    assert sev == sorted(sev, key=("alert", "review", "info").index)   # alertas primero


def test_tampered_subtotal_is_an_alert():
    bad = REPORT.replace("$530.000", "$560.000")
    r = analyze(parse_statements(bad, "x.csv"))
    assert r["checks"]["failed"] == 1
    f = next(f for f in r["findings"] if f["rule"] == "RECONCILIATION")
    assert f["severity"] == "alert" and f["amount"] == "30000" and "MANTENCION" in f["title"]


def test_identical_duplicate_charge_is_alert():
    dup = "descripcion;monto\nInternet;$30.000\nInternet;$30.000\nLuz;$50.000\n"
    r = analyze(parse_statements(dup, "d.csv"))
    f = next(f for f in r["findings"] if f["rule"] == "DUPLICATE_CHARGE")
    assert f["severity"] == "alert" and f["amount"] == "30000"


def test_tabular_comma_file_with_category_and_month_column_splits_and_compares():
    csv_text = (
        'mes,categoria,concepto,monto\n'
        '2025-07,Servicios,Luz,"100.000"\n2025-07,Servicios,Agua,"50.000"\n2025-07,Seguros,Seguro,"40.000"\n'
        '2025-08,Servicios,Luz,"150.000"\n2025-08,Servicios,Agua,"50.000"\n2025-08,Otros,Cerrajero,"60.000"\n'
    )
    sts = parse_statements(csv_text, "multi.csv")
    assert [s.period_key for s in sts] == ["2025-07", "2025-08"]
    r = analyze(sts)
    rules = [(f["rule"], f["title"]) for f in r["findings"]]
    assert any(r_ == "ITEM_CHANGE" and "«Luz» sube 50,0 %" in t for r_, t in rules)
    assert any(r_ == "NEW_ITEM" and "Cerrajero" in t for r_, t in rules)
    assert any(r_ == "DROPPED_ITEM" and "Seguro" in t for r_, t in rules)
    assert r["comparison"]["periods"] == ["2025-07", "2025-08"]
    assert {row["label"] for row in r["comparison"]["rows"]} == {"Luz", "Agua", "Seguro", "Cerrajero"}


def test_periods_are_sorted_chronologically_across_files():
    a = "descripcion;monto\nLuz;$100\nAgua;$50\n"
    sts = parse_statements(a, "AGOSTO 25.csv") + parse_statements(a, "julio 2025.csv")
    r = analyze(sts)
    assert [s["period"] for s in r["statements"]] == ["2025-07", "2025-08"]


def test_same_period_twice_is_flagged():
    a = "descripcion;monto\nLuz;$100\nAgua;$50\n"
    r = analyze(parse_statements(a, "agosto 2025.csv") + parse_statements(a, "AGOSTO 25 (copia).csv"))
    assert any(f["rule"] == "DUPLICATE_PERIOD" and f["severity"] == "alert" for f in r["findings"])


def test_errors():
    with pytest.raises(ExpenseFormatError):
        parse_statements("   \n", "v.csv")
    with pytest.raises(ExpenseFormatError):
        parse_statements("nombre;edad\nAna;30 años\n", "personas.csv")
    with pytest.raises(ExpenseFormatError):
        parse_statements("descripcion;monto\n" + "Luz;$1\n" * 5200, "grande.csv")


def test_formatting_helpers():
    assert clp(Decimal(8644279)) == "$8.644.279" and clp(Decimal("-3920")) == "-$3.920" and clp(Decimal("1234.5")) == "$1.234,50"
    assert pct(Decimal("0.6431")) == "64,3 %"


def test_endpoint_returns_analysis_and_audits_only_metadata(monkeypatch):
    from cloudcost.routers import expenses as router
    from cloudcost.schemas import ExpenseAnalyzeIn

    events = []

    class _Tx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(router, "tenant_tx", lambda org: _Tx())
    monkeypatch.setattr(router.audit, "record", lambda conn, org, ev, **kw: events.append((ev, kw)))
    principal = SimpleNamespace(org_id=uuid4(), user_id="u1")
    out = router.analyze_expenses(ExpenseAnalyzeIn(files=[{"filename": "g.csv", "csv_text": REPORT}]), principal)
    assert out["checks"]["failed"] == 0 and out["statements"][0]["period"] == "2025-08"
    (ev, kw), = events
    assert ev == "EXPENSES_ANALYZED"
    dumped = str(kw["payload"])
    assert "Ana Perez" not in dumped and "Luis Soto" not in dumped and kw["payload"]["items"] == 11

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        router.analyze_expenses(ExpenseAnalyzeIn(files=[{"filename": "malo.csv", "csv_text": "nombre;edad\nAna;treinta"}]), principal)
    assert exc.value.status_code == 422 and exc.value.detail["errors"]
