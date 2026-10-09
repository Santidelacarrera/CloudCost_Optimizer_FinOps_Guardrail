"""Generación de los archivos del informe ejecutivo: Excel (.xlsx, con fórmulas vivas) y PDF (A4, con gráfico).

Reciben el diccionario de `reports.data.build` y no tocan la base de datos, por lo que se prueban sin infraestructura.
Los totales del Excel son fórmulas (SUM/SUMIF/COUNTIF) sobre la hoja «Recomendaciones»: si la gerencia filtra o corrige
una fila, los resúmenes se recalculan.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"

ENV_LABELS = {"production": "Producción", "staging": "Preproducción", "development": "Desarrollo", "test": "Pruebas",
              "unknown": "Sin clasificar"}
METHOD_LABELS = {"cost_records_controlled": "Medido (controlado)", "cost_records": "Costos reales", "manual": "Declarado"}
COST_SOURCE_LABELS = {"cost_explorer": "Real (Cost Explorer)", "import": "Real (archivo importado)",
                      "estimate": "Estimado (tabla de precios)", "estimate_upper_bound": "Estimado (cota superior)",
                      "demo": "Datos de demostración"}


def money(v: float | int | None) -> str:
    """USD 1.234,56 (formato es-ES)."""
    n = f"{float(v or 0):,.2f}"
    return "USD " + n.replace(",", "§").replace(".", ",").replace("§", ".")


def pct(v: float | None, digits: int = 1) -> str:
    return "n/d" if v is None else f"{v:.{digits}f}".replace(".", ",") + " %"


def stamp(dt: datetime) -> str:
    return dt.strftime("%d-%m-%Y %H:%M UTC")


def env_label(env: str | None) -> str:
    return ENV_LABELS.get(env or "unknown", env or "Sin clasificar")


def cost_source_summary(items: list[dict[str, Any]]) -> tuple[int, int]:
    """(con costo real, con costo estimado)."""
    real = sum(1 for i in items if i.get("cost_source") in ("cost_explorer", "import"))
    return real, len(items) - real


# =============================================================================== Excel
def render_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ink, accent, paper = "1F2937", "0F766E", "F8F5EE"
    head_fill = PatternFill("solid", fgColor=ink)
    soft_fill = PatternFill("solid", fgColor=paper)
    hl_fill = PatternFill("solid", fgColor="FDE68A")
    line = Side(style="thin", color="D6D3CB")
    box = Border(bottom=line)
    base = Font(name="Arial", size=10, color=ink)
    bold = Font(name="Arial", size=10, bold=True, color=ink)
    white = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    input_blue = Font(name="Arial", size=10, color="0000FF")          # convención: azul = valor de entrada de la base de datos
    usd_fmt, pct_fmt, int_fmt = '"USD" #,##0.00', "0.0%", "#,##0"

    wb = Workbook()
    items = data["items"]
    n = max(len(items), 1)
    last = n + 1                                                       # última fila de datos en «Recomendaciones»

    # ---------------------------------------------------------------- Recomendaciones (datos)
    ws_r = wb.active
    ws_r.title = "Recomendaciones"
    cols = [("Prioridad", 10), ("Recomendación", 58), ("Regla", 30), ("Recurso", 24), ("Nombre", 24), ("Entorno", 16),
            ("Región", 14), ("Estado", 24), ("Riesgo", 10), ("Costo actual/mes (USD)", 20), ("Costo proyectado/mes (USD)", 22),
            ("Ahorro/mes (USD)", 18), ("Confianza", 11), ("Origen del costo", 28), ("Detectada", 18)]
    for c, (title, width) in enumerate(cols, 1):
        cell = ws_r.cell(row=1, column=c, value=title)
        cell.font, cell.fill = white, head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws_r.column_dimensions[get_column_letter(c)].width = width
    ws_r.row_dimensions[1].height = 32
    for r, i in enumerate(items, 2):
        row = [i["priority"], i["title"], i["rule_label"], i["resource_id"], i.get("resource_name") or "",
               env_label(i["environment"]), i["region"], i["status_label"], i["risk_label"], i["current_monthly_cost"],
               i["projected_monthly_cost"], None, i["confidence"],
               COST_SOURCE_LABELS.get(i["cost_source"], i["cost_source"]), i["created_at"].replace(tzinfo=None)]
        for c, v in enumerate(row, 1):
            cell = ws_r.cell(row=r, column=c, value=v)
            cell.font, cell.border = base, box
        ws_r.cell(row=r, column=12, value=f"=J{r}-K{r}")               # ahorro = costo actual - costo proyectado (fórmula)
        ws_r.cell(row=r, column=12).font = base
        for c in (10, 11, 12):
            ws_r.cell(row=r, column=c).number_format = usd_fmt
        ws_r.cell(row=r, column=13).number_format = "0%"
        ws_r.cell(row=r, column=15).number_format = "dd-mm-yyyy"
        ws_r.cell(row=r, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    if items:
        t = last + 1
        ws_r.cell(row=t, column=2, value="TOTAL").font = bold
        for c in (10, 11, 12):
            col = get_column_letter(c)
            cell = ws_r.cell(row=t, column=c, value=f"=SUM({col}2:{col}{last})")
            cell.font, cell.number_format, cell.fill = bold, usd_fmt, hl_fill if c == 12 else soft_fill
        ws_r.auto_filter.ref = f"A1:O{last}"
    ws_r.freeze_panes = "C2"

    # ---------------------------------------------------------------- Resumen
    ws = wb.create_sheet("Resumen", 0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 70
    ws["A1"] = "Informe ejecutivo de desperdicio y ahorro en la nube"
    ws["A1"].font = Font(name="Arial", size=16, bold=True, color=ink)
    ws["A2"] = data["organization"]
    ws["A2"].font = Font(name="Arial", size=11, color=accent, bold=True)
    ws["A3"] = f"Generado el {stamp(data['generated_at'])}. Cifras mensuales en USD, sin impuestos."
    ws["A3"].font = Font(name="Arial", size=9, italic=True, color="6B7280")
    for c, h in enumerate(("Indicador", "Valor", "Qué significa"), 1):
        cell = ws.cell(row=5, column=c, value=h)
        cell.font, cell.fill = white, head_fill
    k = data["kpi"]
    rows = [
        ("Gasto mensual inventariado", k["monthly_spend"], usd_fmt, "Costo mensual de los recursos activos detectados en el último escaneo.", True),
        ("Ahorro potencial mensual", f"=SUM(Recomendaciones!L2:L{last})", usd_fmt, "Suma del ahorro de las recomendaciones abiertas (hoja «Recomendaciones»).", False),
        ("Ahorro potencial anual", "=B7*12", usd_fmt, "Ahorro mensual × 12, si todas las recomendaciones se aprobaran y desplegaran.", False),
        ("Ahorro sobre el gasto", "=IF(B6>0,B7/B6,0)", pct_fmt, "Ahorro potencial mensual dividido por el gasto mensual inventariado.", False),
        ("Recomendaciones abiertas", f"=COUNTA(Recomendaciones!A2:A{last})" if items else 0, int_fmt, "Pendientes de aprobación, aprobadas o en curso (PR creado, fusionado o desplegado).", False),
        ("Pendientes de aprobación", k["pending_approval"], int_fmt, "Esperan la decisión de una persona; nada se aplica sin ella.", True),
        ("Con riesgo alto", k["high_risk"], int_fmt, "Requieren aprobación reforzada (mínimo 2 aprobadores).", True),
        ("Ahorro verificado (mensual)", k["realized_savings"], usd_fmt, "Ahorro observado en los costos reales tras el despliegue.", True),
        ("Ahorro esperado de lo verificado", k["expected_savings_verified"], usd_fmt, "Lo que se había estimado para las mismas recomendaciones.", True),
        ("Realización del ahorro", '=IF(B14>0,B13/B14,"n/d")', pct_fmt, "Ahorro verificado ÷ esperado. Cerca de 100 % indica estimaciones fiables.", False),
    ]
    for r, (label, value, fmt, note, is_input) in enumerate(rows, 6):
        ws.cell(row=r, column=1, value=label).font = bold
        cell = ws.cell(row=r, column=2, value=value)
        cell.number_format, cell.font = fmt, (input_blue if is_input else base)
        cell.alignment = Alignment(horizontal="right")
        note_cell = ws.cell(row=r, column=3, value=note)
        note_cell.font = Font(name="Arial", size=9, color="6B7280")
        for c in (1, 2, 3):
            ws.cell(row=r, column=c).border = box
    ws["B7"].fill = hl_fill
    ws["B7"].font = Font(name="Arial", size=11, bold=True, color=ink)
    real, est = cost_source_summary(items)
    notes = [
        "Cómo leer este informe",
        "• Azul = dato leído de la plataforma; negro = fórmula calculada en este archivo.",
        f"• Costos: {real} recomendaciones usan costo real (Cost Explorer o archivo importado) y {est} usan una estimación por tabla de precios.",
        "• La plataforma nunca modifica producción: cada cambio se propone como Pull Request y exige aprobación humana.",
    ]
    if data.get("items_truncated"):
        notes.append(f"• Se listan las {len(items)} recomendaciones de mayor ahorro; los totales de la plataforma incluyen todas.")
    for r, text in enumerate(notes, 18):
        ws.cell(row=r, column=1, value=text).font = bold if r == 18 else Font(name="Arial", size=9, color=ink)

    bd = data.get("breakdown")
    if bd:
        r0 = 18 + len(notes) + 2
        ws.cell(row=r0, column=1, value="Estimado · aprobado · observado (tres cifras distintas: no se suman)").font = Font(name="Arial", size=12, bold=True, color=ink)
        for c, h in enumerate(("Cifra", "USD/mes", "Qué es y qué no es"), 1):
            cell = ws.cell(row=r0 + 1, column=c, value=h)
            cell.font, cell.fill = white, head_fill
        obs = bd["observed"]
        lines = [
            (f"Estimado, sin decidir ({bd['estimated']['count']})", bd["estimated"]["monthly"],
             "Ahorro calculado por las reglas para recomendaciones que aún nadie ha aprobado. Es una proyección."),
            (f"Aprobado, sin verificar ({bd['approved']['count']})", bd["approved"]["monthly"],
             "La cifra que vieron quienes aprobaron, congelada. Sigue siendo una proyección hasta que se despliegue y se mida."),
            (f"Observado atribuible al cambio ({obs['attributed_count']})", obs["attributed"],
             "Medido con costos facturados, con períodos alineados y controles de uso. Compatible con el cambio; no prueba causalidad."),
            ("Observado no atribuible solo al cambio", obs["confounded"],
             "Medido con facturación, pero el uso o el resto del servicio se movieron en el mismo período."),
            ("Observado declarado o sin facturación", obs["declared"],
             "Cifra introducida a mano o calculada con la tabla de precios: no es una observación."),
        ]
        for i, (label, value, note) in enumerate(lines, r0 + 2):
            ws.cell(row=i, column=1, value=label).font = bold
            cell = ws.cell(row=i, column=2, value=value)
            cell.number_format, cell.font = usd_fmt, input_blue
            cell.alignment = Alignment(horizontal="right")
            ws.cell(row=i, column=3, value=note).font = Font(name="Arial", size=9, color="6B7280")
            for c in (1, 2, 3):
                ws.cell(row=i, column=c).border = box
        rr = r0 + 2 + len(lines)
        ws.cell(row=rr, column=1, value="Realización (solo observado atribuible)").font = bold
        ws.cell(row=rr, column=2, value=(obs["realization_pct_attributed"] / 100 if obs["realization_pct_attributed"] is not None else "n/d")).number_format = pct_fmt
        ws.cell(row=rr, column=3, value="Observado atribuible ÷ lo aprobado de esas mismas recomendaciones.").font = Font(name="Arial", size=9, color="6B7280")

    # ---------------------------------------------------------------- Desglose
    wd = wb.create_sheet("Desglose", 1)
    wd.sheet_view.showGridLines = False
    for col, w in zip("ABCD", (34, 18, 22, 16), strict=True):
        wd.column_dimensions[col].width = w

    def block(start: int, title: str, labels: list[str], criteria_col: str) -> int:
        wd.cell(row=start, column=1, value=title).font = Font(name="Arial", size=12, bold=True, color=ink)
        for c, h in enumerate((title.split(" por ")[-1].capitalize(), "Recomendaciones", "Ahorro/mes (USD)", "% del total"), 1):
            cell = wd.cell(row=start + 1, column=c, value=h)
            cell.font, cell.fill = white, head_fill
        first, end = start + 2, start + 1 + max(len(labels), 1)
        for r, label in enumerate(labels or ["—"], first):
            wd.cell(row=r, column=1, value=label).font = base
            wd.cell(row=r, column=2, value=f"=COUNTIF(Recomendaciones!${criteria_col}$2:${criteria_col}${last},A{r})").number_format = int_fmt
            wd.cell(row=r, column=3, value=f"=SUMIF(Recomendaciones!${criteria_col}$2:${criteria_col}${last},A{r},Recomendaciones!$L$2:$L${last})").number_format = usd_fmt
            wd.cell(row=r, column=4, value=f"=IF($C${end + 1}>0,C{r}/$C${end + 1},0)").number_format = pct_fmt
            for c in (1, 2, 3, 4):
                wd.cell(row=r, column=c).border = box
                if c > 1:
                    wd.cell(row=r, column=c).font = base
        t = end + 1
        wd.cell(row=t, column=1, value="Total").font = bold
        for c, col in ((2, "B"), (3, "C")):
            cell = wd.cell(row=t, column=c, value=f"=SUM({col}{first}:{col}{end})")
            cell.font, cell.fill = bold, soft_fill
            cell.number_format = int_fmt if c == 2 else usd_fmt
        return t + 2

    nxt = block(1, "Ahorro por regla", [r["label"] for r in data["by_rule"]], "C")
    nxt = block(nxt, "Ahorro por entorno", [env_label(e["environment"]) for e in data["by_environment"]], "F")
    block(nxt, "Ahorro por riesgo", [r["label"] for r in data["by_risk"]], "I")

    # ---------------------------------------------------------------- Ahorro verificado
    wv = wb.create_sheet("Ahorro verificado")
    for c, (h, w) in enumerate((("Recomendación", 58), ("Aprobado/mes (USD)", 20), ("Observado ajustado/mes (USD)", 24),
                                ("Realización", 14), ("Método", 16), ("Ventana", 24), ("Lectura", 30), ("Confianza", 12),
                                ("Diferencia bruta/mes (USD)", 22), ("Otros factores", 40)), 1):
        cell = wv.cell(row=1, column=c, value=h)
        cell.font, cell.fill = white, head_fill
        wv.column_dimensions[get_column_letter(c)].width = w
    for r, v in enumerate(data["verified"], 2):
        wv.cell(row=r, column=1, value=v["title"]).font = base
        wv.cell(row=r, column=2, value=v["expected_monthly_savings"]).number_format = usd_fmt
        wv.cell(row=r, column=3, value=v["observed_monthly_savings"]).number_format = usd_fmt
        wv.cell(row=r, column=4, value=f'=IF(B{r}>0,C{r}/B{r},"n/d")').number_format = "0%"
        wv.cell(row=r, column=5, value=METHOD_LABELS.get(v["method"], v["method"])).font = base
        win = f"{v['window_start']} a {v['window_end']}" if v.get("window_start") else ""
        wv.cell(row=r, column=6, value=win).font = base
        wv.cell(row=r, column=7, value=v.get("attribution_label", "")).font = base
        wv.cell(row=r, column=8, value=v.get("grade_label", "")).font = base
        raw = wv.cell(row=r, column=9, value=v.get("raw_observed_monthly_savings"))
        raw.number_format, raw.font = usd_fmt, input_blue
        wv.cell(row=r, column=10, value=", ".join(v.get("confounders") or [])).font = base
        for c in (2, 3, 4):
            wv.cell(row=r, column=c).font = input_blue if c < 4 else base
    wv.freeze_panes = "A2"

    wb.properties.title = "Informe ejecutivo de desperdicio y ahorro"
    wb.properties.creator = "CloudCost Optimizer"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =============================================================================== PDF
def render_pdf(data: dict[str, Any]) -> bytes:
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ink, teal, mute = colors.HexColor("#1F2937"), colors.HexColor("#0F766E"), colors.HexColor("#6B7280")
    paper, line, hl = colors.HexColor("#F8F5EE"), colors.HexColor("#D6D3CB"), colors.HexColor("#FDE68A")

    def style(name, **kw):
        return ParagraphStyle(name, fontName=kw.pop("fontName", "Helvetica"), textColor=kw.pop("textColor", ink), **kw)

    h1 = style("h1", fontName="Helvetica-Bold", fontSize=22, leading=26)
    h2 = style("h2", fontName="Helvetica-Bold", fontSize=13, leading=16, spaceBefore=16, spaceAfter=6)
    body = style("body", fontSize=9, leading=12.5)
    small = style("small", fontSize=7.5, leading=10, textColor=mute)
    cell = style("cell", fontSize=8, leading=10)
    cell_r = style("cell_r", fontSize=8, leading=10, alignment=TA_RIGHT)
    head = style("head", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white)
    head_r = style("head_r", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white, alignment=TA_RIGHT)
    kpi_v = style("kpi_v", fontName="Helvetica-Bold", fontSize=15, leading=18)
    kpi_l = style("kpi_l", fontSize=7.5, leading=10, textColor=mute)

    class Numbered(canvas.Canvas):
        """Lienzo de dos pasadas para «Página x de y» y pie de confidencialidad."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._saved: list[dict] = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont("Helvetica", 7.5)
                self.setFillColor(mute)
                self.drawString(18 * mm, 10 * mm, f"{data['organization']} · Informe confidencial · Generado {stamp(data['generated_at'])}")
                self.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Página {self._pageNumber} de {total}")
                super().showPage()
            super().save()

    k, items = data["kpi"], data["items"]
    real, est = cost_source_summary(items)
    story: list = [
        Paragraph("Informe ejecutivo de desperdicio y ahorro", h1),
        Spacer(1, 2 * mm),
        Paragraph(f"<b>{_esc(data['organization'])}</b> · {stamp(data['generated_at'])}", body),
        Spacer(1, 6 * mm),
    ]

    def kpi_cell(value: str, label: str):
        return [Paragraph(value, kpi_v), Paragraph(label, kpi_l)]

    kpis = Table([[
        kpi_cell(money(k["potential_savings"]), f"Ahorro potencial al mes<br/>({pct(k['savings_pct'])} del gasto)"),
        kpi_cell(money(k["potential_savings"] * 12), "Ahorro potencial anual"),
        kpi_cell(money(k["monthly_spend"]), "Gasto mensual inventariado"),
        kpi_cell(money(k["realized_savings"]), "Ahorro ya verificado" + (f"<br/>({pct(k['realization_pct'], 0)} de lo esperado)" if k["realization_pct"] is not None else "")),
    ]], colWidths=[(A4[0] - 36 * mm) / 4] * 4)
    kpis.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), hl), ("BACKGROUND", (1, 0), (-1, 0), paper),
                              ("BOX", (0, 0), (-1, -1), 0.5, line), ("INNERGRID", (0, 0), (-1, -1), 0.5, line),
                              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [kpis, Spacer(1, 4 * mm)]
    bd = data.get("breakdown")
    if bd:
        obs = bd["observed"]
        rows = [[Paragraph("Cifra (nunca se suman entre sí)", head), Paragraph("Recom.", head_r), Paragraph("USD/mes", head_r)],
                [Paragraph("<b>Estimado</b>, sin decidir: proyección de las reglas", cell), Paragraph(str(bd["estimated"]["count"]), cell_r),
                 Paragraph(money(bd["estimated"]["monthly"]), cell_r)],
                [Paragraph("<b>Aprobado</b>, sin verificar: la cifra que vieron quienes aprobaron", cell), Paragraph(str(bd["approved"]["count"]), cell_r),
                 Paragraph(money(bd["approved"]["monthly"]), cell_r)],
                [Paragraph("<b>Observado</b> atribuible al cambio (facturación, períodos alineados, controles de uso)", cell),
                 Paragraph(str(obs["attributed_count"]), cell_r), Paragraph(money(obs["attributed"]), cell_r)],
                [Paragraph("Observado no atribuible solo al cambio (otros factores se movieron)", cell), Paragraph("", cell_r),
                 Paragraph(money(obs["confounded"]), cell_r)],
                [Paragraph("Observado declarado o sin facturación (no es una observación)", cell), Paragraph("", cell_r),
                 Paragraph(money(obs["declared"]), cell_r)]]
        story += [Spacer(1, 4 * mm), Paragraph("Estimado, aprobado y observado", h2),
                  _table(rows, [110 * mm, 20 * mm, 40 * mm], ink, line, paper),
                  Paragraph("El ahorro <b>observado</b> compara la facturación de las 2 semanas previas con la de las semanas posteriores al despliegue, "
                            "sin los días de transición ni los últimos con retraso, y corrige por los días en que el recurso estuvo activo. "
                            "Es compatible con el cambio pero <b>no demuestra que lo causara</b>: compromisos (Reserved Instances, Savings Plans), "
                            "créditos y cambios de carga pueden mover el coste por su cuenta.", small)]
    story.append(Paragraph(
        f"Hay <b>{k['recommendations']}</b> recomendaciones abiertas: <b>{k['pending_approval']}</b> esperan aprobación y "
        f"<b>{k['high_risk']}</b> son de riesgo alto (requieren aprobación reforzada). Ningún cambio se aplica sin Pull Request y "
        "aprobación humana.", body))

    # ---- desglose por regla (gráfico + tabla)
    story.append(Paragraph("Dónde está el desperdicio", h2))
    rules = data["by_rule"]
    if rules:
        n = len(rules)
        d = Drawing(170 * mm, 14 * mm * n + 12 * mm)
        bc = HorizontalBarChart()
        bc.x, bc.y, bc.width, bc.height = 62 * mm, 4 * mm, 96 * mm, 14 * mm * n
        bc.data = [[r["savings"] for r in reversed(rules)]]
        bc.categoryAxis.categoryNames = [r["label"] for r in reversed(rules)]
        bc.categoryAxis.labels.fontName, bc.categoryAxis.labels.fontSize, bc.categoryAxis.labels.dx = "Helvetica", 8, -2
        bc.categoryAxis.strokeColor = line
        bc.valueAxis.labels.fontName, bc.valueAxis.labels.fontSize = "Helvetica", 7
        bc.valueAxis.valueMin = 0
        bc.valueAxis.strokeColor = line
        bc.valueAxis.visibleGrid, bc.valueAxis.gridStrokeColor = True, line
        bc.bars[0].fillColor, bc.bars[0].strokeColor = teal, teal
        bc.barWidth = 9
        bc.barLabelFormat = lambda v: money(v)
        bc.barLabels.fontName, bc.barLabels.fontSize, bc.barLabels.nudge = "Helvetica", 7, 20
        d.add(bc)
        story.append(d)
        total = sum(r["savings"] for r in rules) or 1
        rows = [[Paragraph("Regla", head), Paragraph("Recom.", head_r), Paragraph("Ahorro/mes", head_r), Paragraph("%", head_r)]]
        rows += [[Paragraph(_esc(r["label"]), cell), Paragraph(str(r["count"]), cell_r), Paragraph(money(r["savings"]), cell_r),
                  Paragraph(pct(r["savings"] / total * 100, 0), cell_r)] for r in rules]
        story.append(_table(rows, [80 * mm, 20 * mm, 40 * mm, 20 * mm], ink, line, paper))
    else:
        story.append(Paragraph("Sin recomendaciones abiertas en este momento.", body))

    # ---- entorno y riesgo
    if data["by_environment"] or data["by_risk"]:
        story.append(Paragraph("Por entorno y por riesgo", h2))
        env_rows = [[Paragraph("Entorno", head), Paragraph("Recom.", head_r), Paragraph("Ahorro/mes", head_r)]]
        env_rows += [[Paragraph(env_label(e["environment"]), cell), Paragraph(str(e["count"]), cell_r), Paragraph(money(e["savings"]), cell_r)]
                     for e in data["by_environment"]]
        risk_rows = [[Paragraph("Riesgo", head), Paragraph("Recom.", head_r), Paragraph("Ahorro/mes", head_r)]]
        risk_rows += [[Paragraph(r["label"], cell), Paragraph(str(r["count"]), cell_r), Paragraph(money(r["savings"]), cell_r)]
                      for r in data["by_risk"]]
        two = Table([[_table(env_rows, [34 * mm, 14 * mm, 28 * mm], ink, line, paper),
                      _table(risk_rows, [34 * mm, 14 * mm, 28 * mm], ink, line, paper)]], colWidths=[85 * mm, 85 * mm])
        two.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(two)

    # ---- mayores oportunidades
    if items:
        story.append(Paragraph("Mayores oportunidades", h2))
        top = items[:15]
        rows = [[Paragraph("Recomendación", head), Paragraph("Entorno", head), Paragraph("Riesgo", head),
                 Paragraph("Estado", head), Paragraph("Ahorro/mes", head_r)]]
        for i in top:
            rows.append([Paragraph(_esc(i["title"]), cell), Paragraph(env_label(i["environment"]), cell),
                         Paragraph(f"<font color='{'#B91C1C' if i['risk'] == 'HIGH' else '#1F2937'}'>{i['risk_label']}</font>", cell),
                         Paragraph(_esc(i["status_label"]), cell), Paragraph(money(i["estimated_monthly_savings"]), cell_r)])
        story.append(_table(rows, [66 * mm, 24 * mm, 16 * mm, 30 * mm, 34 * mm], ink, line, paper))
        if len(items) > len(top):
            story.append(Paragraph(f"Se muestran las {len(top)} mayores de {len(items)}{'+' if data.get('items_truncated') else ''}. "
                                   "El detalle completo está en el Excel.", small))

    # ---- verificado
    if data["verified"]:
        story.append(Paragraph("Ahorro verificado tras el despliegue", h2))
        rows = [[Paragraph("Recomendación", head), Paragraph("Aprobado/mes", head_r), Paragraph("Observado/mes", head_r),
                 Paragraph("Real.", head_r), Paragraph("Lectura", head)]]
        for v in data["verified"][:10]:
            rows.append([Paragraph(_esc(v["title"]), cell), Paragraph(money(v["expected_monthly_savings"]), cell_r),
                         Paragraph(money(v["observed_monthly_savings"]), cell_r), Paragraph(pct(v["realization_pct"], 0), cell_r),
                         Paragraph(_esc(v.get("attribution_label", METHOD_LABELS.get(v["method"], ""))), cell)])
        story.append(_table(rows, [56 * mm, 26 * mm, 26 * mm, 16 * mm, 46 * mm], ink, line, paper))

    # ---- método y límites
    story.append(KeepTogether([
        Paragraph("Cómo se calcularon estas cifras", h2),
        Paragraph(f"{real} recomendaciones usan <b>costo real</b> (Cost Explorer o archivo importado) y {est} una <b>estimación</b> por tabla de "
                  "precios; confirma estas últimas con la factura antes de aprobar. El ahorro de los snapshots es una cota superior "
                  "(los snapshots son incrementales). Las cifras son mensuales, en USD y sin impuestos.", body),
        Spacer(1, 2 * mm),
        Paragraph("El ahorro proyectado solo se materializa cuando el Pull Request se aprueba, se fusiona y se despliega; el ahorro "
                  "<i>verificado</i> se mide después con los costos reales.", body),
    ]))

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title="Informe ejecutivo de desperdicio y ahorro", author="CloudCost Optimizer",
                            subject=f"{data['organization']}")
    doc.build(story, canvasmaker=Numbered)
    return buf.getvalue()


def _esc(text: Any) -> str:
    # Helvetica (WinAnsi) no tiene flechas ni signos matemáticos: se sustituyen para no mostrar cuadros vacíos.
    text = str(text or "").replace("→", "->").replace("≥", ">=").replace("≤", "<=")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table(rows, widths, ink, line, paper):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ink), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, paper]), ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]))
    return t
