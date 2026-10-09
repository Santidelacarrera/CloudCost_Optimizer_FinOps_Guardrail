"""El informe de demostración publicado está al día y respeta sus propias reglas: las tres cifras no se mezclan y los límites se declaran."""
from __future__ import annotations

from pathlib import Path

from cloudcost import demo_report

PUBLISHED = Path(__file__).resolve().parents[2] / "docs" / "demo" / "informe-ahorro-demo.md"


def test_published_report_matches_what_the_code_generates():
    assert PUBLISHED.read_text(encoding="utf-8") == demo_report.build(), "regenera: python -m cloudcost.demo_report > docs/demo/informe-ahorro-demo.md"


def test_report_is_deterministic():
    assert demo_report.build() == demo_report.build()


def test_report_says_it_is_synthetic_and_states_its_limits():
    text = demo_report.build()
    assert "DATOS SINTÉTICOS DE LABORATORIO" in text and "No son clientes" in text
    assert "no demuestra que lo causara" in text and "No demuestra ahorros reales" in text
    assert "Ningún método basado en facturación puede probar causalidad" in text


def test_every_kind_of_outcome_is_illustrated_and_kept_in_its_own_bucket():
    text = demo_report.build()
    for reading in ("Coherente con el cambio", "El cambio no parece aplicado", "No atribuible solo al cambio", "No concluyente", "Declarado, no medido",
                    "Aprobado, sin desplegar", "Pendiente de aprobación"):
        assert reading in text, reading
    totals = text.split("## Totales: cada tipo de cifra por separado")[1].split("## Detalle")[0]
    assert totals.count("|") > 20 and "Observado atribuible al cambio" in totals and "Observado declarado o sin facturación" in totals


def test_the_hand_written_and_price_table_cases_never_count_as_observed():
    text = demo_report.build()
    totals = text.split("## Totales: cada tipo de cifra por separado")[1]
    attributed = [ln for ln in totals.splitlines() if ln.startswith("| **Observado atribuible")][0]
    assert "H" not in attributed.split("|")[-2] and "I" not in attributed.split("|")[-2]
    declared = [ln for ln in totals.splitlines() if ln.startswith("| Observado declarado")][0]
    assert "H" in declared and "I" in declared
