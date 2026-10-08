"""Reportes ejecutivos: los totales de Excel y PDF salen de los mismos datos y las fórmulas apuntan a las filas correctas."""
import io
from datetime import datetime, timezone

import pytest
from cloudcost.services import reports as R
from openpyxl import load_workbook

NOW = datetime(2026, 10, 8, 14, 5, tzinfo=timezone.utc)


def opp(title, savings, rule="Volúmenes huérfanos", env="Producción", src="cost_explorer"):
    return {"title": title, "resource_id": "r-" + title, "rule": rule, "action": "DELETE_VOLUME", "environment": env, "risk": "MEDIUM",
            "priority": "P2", "monthly_savings": savings, "confidence": 0.9, "status": "Pendiente de aprobación",
            "cost_source": src, "destructive": True}


@pytest.fixture
def data():
    return R.ReportData("Acme", NOW, 1000.0, [opp("a", 300.0), opp("b", 100.5, rule="Instancias ociosas", env="Staging", src="estimate"),
                                              opp("c", 50.0, env="Staging")],
                        [("2026-10-01", 30.0), ("2026-10-02", 32.5)],
                        [{"title": "x", "expected": 100.0, "observed": 80.0, "pct": 80.0}])


def test_aggregates_are_derived_from_opportunities(data):
    assert data.potential_monthly == 450.5 and data.potential_annual == 5406.0 and data.savings_pct == 45.1
    assert data.verified_monthly == 80.0
    assert data.grouped("rule") == [("Volúmenes huérfanos", 2, 350.0), ("Instancias ociosas", 1, 100.5)]
    assert data.real_cost_share == (2, 3)


def test_headline_names_the_biggest_opportunity(data):
    h = R.headline(data)
    assert "3 oportunidades" in h and "USD 450.50" in h and "«a»" in h
    assert "No hay oportunidades" in R.headline(R.ReportData("Acme", NOW, 0.0))


def test_xlsx_has_sheets_values_and_formulas(data):
    wb = load_workbook(io.BytesIO(R.render_xlsx(data)))
    assert wb.sheetnames == ["Resumen", "Oportunidades", "Por categoría", "Gasto diario", "Ahorro verificado", "Notas"]
    ws = wb["Oportunidades"]
    assert [ws[f"H{r}"].value for r in (2, 3, 4)] == [300.0, 100.5, 50.0]
    assert ws["I2"].value == "=H2*12" and ws["L3"].value == "Estimado" and ws["L2"].value.startswith("Facturado")
    s = wb["Resumen"]
    assert s["B7"].value == 1000.0 and s["B8"].value == "=SUM(Oportunidades!H2:H4)" and s["B9"].value == "=B8*12"
    assert s["B11"].value == "=COUNTA(Oportunidades!A2:A4)"
    assert wb["Por categoría"]["C2"].value == "=SUMIF(Oportunidades!C2:C4,A2,Oportunidades!H2:H4)"


def test_xlsx_with_no_data_still_opens():
    wb = load_workbook(io.BytesIO(R.render_xlsx(R.ReportData("Acme", NOW, 0.0))))
    assert wb["Resumen"]["B8"].value == "=SUM(Oportunidades!H2:H2)"


def test_pdf_is_a_valid_document(data):
    pdf = R.render_pdf(data)
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF") and len(pdf) > 2000
    assert R.render_pdf(R.ReportData("Acme", NOW, 0.0)).startswith(b"%PDF-")      # sin oportunidades tampoco falla


def test_pdf_handles_many_rows_and_long_titles(data):
    data.opportunities = [opp("x" * 120 + str(i), 10.0 + i) for i in range(60)]
    assert R.render_pdf(data).startswith(b"%PDF-")
    assert len(data.opportunities) == 60


def test_spanish_date():
    assert R.long_date(NOW) == "8 de octubre de 2026"
