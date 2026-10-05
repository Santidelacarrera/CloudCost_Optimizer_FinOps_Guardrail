"""Informes jerárquicos (gasto común con detalle por documento) y estados de pago de obra. Todo con datos ficticios."""
from decimal import Decimal

import pytest
from cloudcost.expenses import analyze_documents
from cloudcost.expenses.analyzer import analyze
from cloudcost.expenses.parser import ExpenseFormatError, parse_amount, parse_statements, read_rows
from cloudcost.expenses.project_flow import analyze_project_flow, looks_like_project_flow, parse_project_flow

# Réplica ficticia de un gasto común con códigos 1. / 1.1. / 1.1.1., columnas de proveedor, documento y descripción,
# totales al final y fondo de reserva con porcentaje en la etiqueta.
HIER = """Gasto Común Mes de Agosto de 2026;;;;;;
EDIFICIO EJEMPLO;;;;;;
Fecha de Impresión: 05-10-2026;;;;;;
;;;;;;
Item;Nombre;N° Documento;Fecha;Descripción;VALOR $;TOTAL $
1. ;Remuneraciones;;;;;$1.500.000
1.1. ;  Leyes Sociales;;;;$300.000;
1.1.1.;    AFP Uno;;;;$200.000;
1.1.2.;    AFP Dos;;;;$100.000;
1.2. ;  Sueldo Liquido;;;;$1.200.000;
1.2.1.;    Perez Ana;;31-08-2026;;$700.000;
1.2.2.;    Soto Luis;;31-08-2026;;$500.000;
2. ;Servicios;;;;;$1.436.000
2.1. ;  Agua;;;;$1.036.000;
2.1.1.;    Gas Sur;100;31-08-2026;Agua caliente;$800.000;
2.1.2.;    Aguas Norte S.A;1;31-08-2026;Agua espacios comunes;$3.000;
2.1.3.;    Aguas Norte S.A;2;31-08-2026;Agua espacios comunes;$2.000;
2.1.4.;    Aguas Norte S.A;3;31-08-2026;Agua espacios comunes;$4.000;
2.1.5.;    Aguas Norte S.A;4;31-08-2026;Agua espacios comunes;$227.000;
2.2. ;  Electricidad;;;;$400.000;
2.2.1.;    Luz SA;7;31-08-2026;Electricidad espacios comunes;$400.000;
3. ;Caja menor;;;;;$0
4. ;Seguro;;;;;$100.000
4.1. ;  Seguros Generales;;;;$100.000;
4.1.1.;    Aseguradora Sur;9;31-08-2026;Seguros espacios comunes;$100.000;
TOTAL GASTOS COMUNES;;;;;;$3.036.000
FONDO RESERVA PRINCIPAL (5,00%);;;;;$151.800;
TOTAL GENERAL A COBRAR;;;;;;$3.187.800
;;;;;;
Observaciones;;;;;;
"Estimados residentes: pagar hasta el 15 de septiembre.
Cuenta corriente 000 (dato de ejemplo)
Saludos";;;;;;
"""


def test_hierarchical_report_uses_leaves_only_and_matches_grand_total():
    (st,) = parse_statements(HIER, "gc.csv")
    assert st.period_key == "2026-08"
    assert sum(i.amount for i in st.items) == Decimal(3187800)            # sin contar resúmenes de grupo (sería el doble)
    assert not any(i.label[:1].isdigit() for i in st.items)                # etiquetas = nombre, no el código «1.1.1.»
    agua = next(i for i in st.items if i.doc == "4")
    assert (agua.section, agua.group, agua.note, agua.date) == ("Servicios", "Agua", "Agua espacios comunes", "31-08-2026")
    assert [t.kind for t in st.totals].count("group") == 8
    assert all(t.expected is not None for t in st.totals if t.kind == "group")


def test_hierarchical_checks_all_pass_and_findings():
    r = analyze(parse_statements(HIER, "gc.csv"))
    assert r["checks"] == {"passed": 11, "failed": 0}                      # 8 resúmenes + 2 totales + 5 % del fondo
    rules = {f["rule"]: f for f in r["findings"]}
    out = rules["VENDOR_OUTLIER"]
    assert out["severity"] == "review" and "Aguas Norte" in out["title"] and "$227.000" in out["detail"] and "doc 4" in out["detail"]
    assert "REPEATED_ITEM" not in rules and "DUPLICATE_CHARGE" not in rules  # facturas distintas del mismo proveedor: no es duplicado
    assert rules["DOMINANT_ITEM"]["title"].startswith("«Gas Sur» domina «Agua»")


def test_tampered_group_and_reserve_fund_are_alerts():
    r = analyze(parse_statements(HIER.replace("2.1. ;  Agua;;;;$1.036.000;", "2.1. ;  Agua;;;;$1.000.000;"), "gc.csv"))
    f = next(f for f in r["findings"] if f["rule"] == "RECONCILIATION" and "«Agua»" in f["title"])
    assert f["severity"] == "alert" and f["amount"] == "36000"
    assert r["checks"]["failed"] == 2                                       # el resumen de Agua y, en cascada, el de Servicios
    r = analyze(parse_statements(HIER.replace("$151.800;", "$140.000;"), "gc.csv"))
    f = next(f for f in r["findings"] if f["rule"] == "RESERVE_FUND")
    assert f["severity"] == "alert" and "5,0 %" in f["title"]


def test_same_document_number_twice_is_a_duplicate_charge():
    dup = HIER.replace("2.2.1.;    Luz SA;7;", "2.2.1.;    Luz SA;7;").replace(
        "2.1.2.;    Aguas Norte S.A;1;", "2.1.2.;    Aguas Norte S.A;4;")
    r = analyze(parse_statements(dup, "gc.csv"))
    f = next(f for f in r["findings"] if f["rule"] == "DUPLICATE_CHARGE")
    assert f["severity"] == "alert" and "N° 4" in f["title"]


def test_amounts_in_first_column_are_not_mistaken_for_codes():
    (st,) = parse_statements("monto;descripcion\n1.016;Luz\n2.500;Agua\n3.000;Gas\n", "x.csv")
    assert [i.amount for i in st.items] == [Decimal(1016), Decimal(2500), Decimal(3000)]


def test_dissimilar_statements_are_not_compared_item_by_item():
    a = "descripcion;monto\nLuz;$100\nAgua;$50\nAseo;$30\n"
    b = "descripcion;monto\nPiscina;$900\nCaldera;$800\nGuardia;$700\n"
    r = analyze(parse_statements(a, "julio 2025.csv") + parse_statements(b, "agosto 2025.csv"))
    assert r["comparison"] is None
    assert any(f["rule"] == "COMPARISON_MISMATCH" for f in r["findings"])
    assert not any(f["rule"] in ("ITEM_CHANGE", "NEW_ITEM", "DROPPED_ITEM") for f in r["findings"])


# ---------------------------------------------------------------- estado de pago de obra

def _uf(v):
    return "UF " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pc(v):
    return f"{v:.2f}".replace(".", ",") + "%"


def _block(av, contract=1000.0):
    if av is None:
        return [""] * 5
    dev = round(av * 0.10, 2)
    return [_pc(av / contract * 100), _uf(av), "-", _uf(dev), _uf(round(av - dev, 2))]


MONTHS = ["ene-26", "feb-26", "mar-26", "abr-26", "may-26", "jun-26"]
CONTRACT_FLOW = [100.0, 200.0, 200.0, 200.0, 200.0, 100.0]
PROJECTED = [150.0, 200.0, 200.0, 150.0, 150.0, 150.0]
REAL = [100.0, 150.0, 120.0, None, None, None]


def ep_csv(acc_override=None, project_real=REAL):
    rows = [";;;;;Flujo Financiero Obra EJEMPLO"]
    rows += ["", "Contrato;UF 1.000,00", "Anticipo;10%;UF 100,00", "Retenciones;0%;UF 0,00", ""]
    rows.append(";;Flujo Financiero Contrato;;;;;Flujo Proyectado Actualizado ;;;;;Flujo Real;;;;;Venc Boletas;;Boletas de anticipo;Devol Anticipo")
    rows.append("Nº;Mes;" + ";".join(["Avance %;Avance;Retenciones;Dev Anticipo;Total"] * 3) + ";Póliza;Monto;acumuladas;Acumulada")
    adv = ["10,00%;UF 100,00;UF 0,00;UF 0,00;UF 100,00"] * 3
    rows.append("0;Anticipo;" + ";".join(adv) + ";;;;")
    acc = 0.0
    for n, (m, c, p, r) in enumerate(zip(MONTHS, CONTRACT_FLOW, PROJECTED, project_real), start=1):
        cells = _block(c) + _block(p) + _block(r)
        acc_cell = ""
        if r is not None:
            acc += round(r * 0.10, 2)
            acc_cell = _uf((acc_override or {}).get(n, acc))
        rows.append(f"{n};{m};" + ";".join(cells) + f";;;;{acc_cell}")
    tot_real = sum(x for x in project_real if x)
    rows += ["", ";;" + ";".join([
        f"100,00%;{_uf(1000)};;{_uf(100)};{_uf(1000)}", f"100,00%;{_uf(1000)};;{_uf(100)};{_uf(1000)}",
        f"{_pc(tot_real / 10)};{_uf(tot_real)};UF 0,00;{_uf(tot_real * 0.1)};{_uf(tot_real * 0.9)}"]), "", "AVANCES ACUMULADOS",
        "EP;MES;PROYECTADO ;REAL;DIFERENCIA;;PROYECTADO INICIAL"]
    cp = cr = 0.0
    for n, (m, p, r) in enumerate(zip(MONTHS, PROJECTED, project_real), start=1):
        cp += p
        if r is not None:
            cr += r
            rows.append(f"{n};{m};{_uf(cp)};{_uf(cr)};{_uf(cp - cr)};;{_uf(cp)}")
        else:
            rows.append(f"{n};{m};{_uf(cp)};;;;{_uf(cp)}")
    return "\n".join(rows) + "\n"


def test_uf_amounts():
    assert parse_amount("UF 6.756,91") == Decimal("6756.91") and parse_amount("-UF 499,56") == Decimal("-499.56")


def test_project_flow_detection_and_parse():
    rows = read_rows(ep_csv())
    assert looks_like_project_flow(rows)
    assert not looks_like_project_flow(read_rows(HIER))
    flow = parse_project_flow(rows, "obra.csv")
    assert flow.currency == "UF" and flow.contract == Decimal(1000) and flow.advance_pct == Decimal("0.1")
    assert {k: len(v) for k, v in flow.blocks.items()} == {"contract": 7, "projected": 7, "real": 7}
    assert [a["n"] for a in flow.accumulated] == [1, 2, 3, 4, 5, 6]
    assert set(flow.totals) == {"contract", "projected", "real"}


def test_project_flow_consistent_file_has_all_checks_ok_and_measures_delay():
    res = analyze_project_flow(parse_project_flow(read_rows(ep_csv()), "obra.csv"))
    assert all(c["ok"] for c in res["checks"]), [c for c in res["checks"] if not c["ok"]]
    s = res["summary"]
    assert s["last_ep"] == 3 and Decimal(s["real_cumulative"]) == 370 and Decimal(s["projected_cumulative"]) == 550
    assert Decimal(s["gap"]) == 180 and Decimal(s["gap_share"]) == Decimal("0.18") and Decimal(s["remaining"]) == 630
    rules = {f["rule"]: f for f in res["findings"]}
    assert rules["FLOW_BEHIND"]["severity"] == "alert" and "UF 180,00" in rules["FLOW_BEHIND"]["title"]
    assert rules["FLOW_TREND"]["amount"] == "180.00"                        # 50 + 50 + 80 bajo lo proyectado en los 3 meses
    assert s["eta"] == "2026-09" and s["planned_end"] == "2026-06" and "FLOW_FINISH" in rules
    assert "FLOW_RETENTION" in rules and "FLOW_ADVANCE_ACC" not in rules


def test_project_flow_detects_accumulated_advance_return_formula_error():
    # Réplica del error real: el acumulado del EP 3 suma el Total pagado del mes (108) en vez de la devolución (12)
    res = analyze_project_flow(parse_project_flow(read_rows(ep_csv(acc_override={3: 25.0 + 108.0})), "obra.csv"))
    f = next(f for f in res["findings"] if f["rule"] == "FLOW_ADVANCE_ACC")
    assert f["severity"] == "alert" and "imposible" in f["title"]            # 133 > anticipo de 100
    assert "supera el anticipo" in f["detail"] and "fórmula" in f["detail"] and "UF 108,00" in f["detail"]
    assert any(not c["ok"] and "acumulada" in c["label"] for c in res["checks"])


def test_project_flow_arithmetic_error_is_flagged():
    lines = ep_csv().split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith("2;feb-26;"))
    lines[i] = lines[i].replace("UF 135,00", "UF 140,00")                  # en esa fila, «UF 135,00» es solo el Total real del EP 2 (150 − 15)
    bad = "\n".join(lines)
    res = analyze_project_flow(parse_project_flow(read_rows(bad), "obra.csv"))
    assert any(f["rule"] == "FLOW_NET" and f["severity"] == "alert" and "EP 2" in f["title"] for f in res["findings"])


def test_analyze_documents_dispatches_and_merges():
    result, err = analyze_documents([("gc.csv", HIER), ("obra.csv", ep_csv())])
    assert not err and len(result["statements"]) == 1 and len(result["projects"]) == 1
    assert result["checks"]["failed"] == 0 and result["checks"]["passed"] == 11 + len(result["projects"][0]["checks"])
    rules = {f["rule"] for f in result["findings"]}
    assert {"VENDOR_OUTLIER", "FLOW_BEHIND"} <= rules
    sev = [f["severity"] for f in result["findings"]]
    assert sev == sorted(sev, key=("alert", "review", "info").index)
    _, err = analyze_documents([("malo.csv", "nombre;edad\nAna;treinta\n")])
    assert err["errors"] and "malo.csv" in err["errors"][0]


def test_project_flow_without_real_block_is_rejected():
    with pytest.raises(ExpenseFormatError):
        parse_project_flow(read_rows("Contrato;UF 10,00\nAvance %;Avance;Avance %;Avance\n"), "x.csv")
