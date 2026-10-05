"""Estados de pago / flujo financiero de obra (montos en UF u otra moneda): avance real vs proyectado, anticipo, retenciones.

Formato esperado (el típico de una planilla de contratista): parámetros del contrato (Contrato, Anticipo, Retenciones),
luego bloques de columnas «Avance % · Avance · Retenciones · Dev Anticipo · Total» repetidos para el flujo del contrato,
el flujo proyectado actualizado y el flujo real, una fila por mes (Nº, Mes) y, opcionalmente, una tabla de avances acumulados.

Todo es determinista y aritmético: se comprueba que las cifras de la planilla cuadren entre sí y se mide el atraso. La proyección
de término asume el ritmo reciente y es una estimación, no un compromiso.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .parser import ExpenseFormatError, detect_period, norm, parse_amount

ZERO = Decimal(0)
_PCT_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*%\s*$")
_INT_RE = re.compile(r"^\d{1,3}$")
_TOL = Decimal("0.05")          # tolerancia en la unidad del archivo (UF): redondeos de la planilla
_ROLE_WORDS = (("contract", "contrato"), ("projected", "proyect"), ("real", "real"))


@dataclass
class MonthRow:
    n: int
    label: str
    period: tuple[int, int] | None
    pct: Decimal | None = None          # fracción (0,0208 = 2,08 %)
    advance: Decimal | None = None      # «Avance»
    retention: Decimal | None = None
    advance_return: Decimal | None = None
    net: Decimal | None = None


@dataclass
class ProjectFlow:
    filename: str
    currency: str
    contract: Decimal
    advance_pct: Decimal | None
    advance_amount: Decimal | None
    retention_pct: Decimal | None
    blocks: dict[str, list[MonthRow]] = field(default_factory=dict)       # contract | projected | real
    totals: dict[str, MonthRow] = field(default_factory=dict)
    accumulated: list[dict] = field(default_factory=list)                  # {n, label, projected, real, difference}
    advance_return_accumulated: dict[int, Decimal | None] = field(default_factory=dict)   # columna «Devol Anticipo · Acumulada»


def parse_percent(raw: str | None) -> Decimal | None:
    m = _PCT_RE.match(raw or "")
    return Decimal(m[1].replace(",", ".")) / 100 if m else None


def _money(raw: str | None) -> Decimal | None:
    if raw is None or not raw.strip():
        return None
    if raw.strip() in ("-", "—", "–"):
        return ZERO
    return parse_amount(raw)


def _cur(raw: str) -> str | None:
    r = raw.strip().lower().lstrip("-")
    return "UF" if r.startswith("uf") else ("$" if r.startswith("$") else None)


def looks_like_project_flow(rows: list[list[str]]) -> bool:
    for r in rows[:40]:
        if sum(1 for c in r if norm(c) == "avance %") >= 2 and any("anticipo" in norm(c) for c in r):
            return True
    return False


def _cell(r: list[str], i: int | None) -> str:
    return r[i] if i is not None and 0 <= i < len(r) else ""


def parse_project_flow(rows: list[list[str]], filename: str = "") -> ProjectFlow:
    hdr = next((i for i, r in enumerate(rows[:40]) if sum(1 for c in r if norm(c) == "avance %") >= 2), None)
    if hdr is None:
        raise ExpenseFormatError("No se reconoce el flujo de obra: faltan los bloques «Avance % · Avance · Dev Anticipo · Total»")
    starts = [j for j, c in enumerate(rows[hdr]) if norm(c) == "avance %"]

    # Parámetros del contrato (antes del encabezado)
    contract = adv_pct = adv_amt = ret_pct = None
    currency = "UF"
    for r in rows[:hdr]:
        key = norm(r[0]) if r else ""
        vals = [c for c in r[1:] if c]
        amounts = [(c, parse_amount(c)) for c in vals if parse_amount(c) is not None and _PCT_RE.match(c) is None]
        pcts = [parse_percent(c) for c in vals if parse_percent(c) is not None]
        if key == "contrato" and amounts:
            contract, currency = amounts[0][1], _cur(amounts[0][0]) or currency
        elif key == "anticipo":
            adv_pct = pcts[0] if pcts else None
            adv_amt = amounts[0][1] if amounts else None
        elif key.startswith("retencion"):
            ret_pct = pcts[0] if pcts else None
    if contract is None:
        raise ExpenseFormatError("No se encontró el monto del contrato (fila «Contrato; UF …»)")

    # Roles de bloque (contrato / proyectado / real) por el título de la fila superior
    title_row = rows[hdr - 1] if hdr > 0 else []
    roles: dict[int, str] = {}
    for k, c0 in enumerate(starts):
        title = norm(_cell(title_row, c0))
        role = next((role for role, word in _ROLE_WORDS if word in title), None)
        roles[c0] = role or (("contract", "projected", "real")[k] if k < 3 else f"extra{k}")
    if "real" not in roles.values():
        raise ExpenseFormatError("No se encontró el bloque «Flujo Real»")

    cols: dict[int, dict[str, int]] = {}
    for k, c0 in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(rows[hdr])
        m: dict[str, int] = {}
        for j in range(c0, end):
            h = norm(_cell(rows[hdr], j))
            for key, test in (("pct", h == "avance %"), ("advance", h == "avance"), ("retention", h.startswith("retencion")),
                              ("advance_return", h.startswith("dev") and "anticipo" in h), ("net", h == "total")):
                if test and key not in m:
                    m[key] = j
        cols[c0] = m

    acc_col = next((j for j, c in enumerate(rows[hdr]) if norm(c) == "acumulada"), None)
    flow = ProjectFlow(filename, currency, contract, adv_pct, adv_amt, ret_pct,
                       blocks={roles[c0]: [] for c0 in starts})
    last = hdr
    for i in range(hdr + 1, len(rows)):
        r = rows[i]
        if not any(r):
            if flow.blocks["real"]:
                last = i
                if i + 1 < len(rows) and not any(rows[i + 1]):
                    break
            continue
        if not _INT_RE.match(_cell(r, 0)):
            if flow.blocks["real"]:
                last = i
                break
            continue
        label = _cell(r, 1)
        for c0 in starts:
            c = cols[c0]
            flow.blocks[roles[c0]].append(MonthRow(
                int(r[0]), label, detect_period(label), parse_percent(_cell(r, c.get("pct"))),
                _money(_cell(r, c.get("advance"))), _money(_cell(r, c.get("retention"))),
                _money(_cell(r, c.get("advance_return"))), _money(_cell(r, c.get("net")))))
        if acc_col is not None:
            flow.advance_return_accumulated[int(r[0])] = _money(_cell(r, acc_col))
        last = i
    if not flow.blocks["real"]:
        raise ExpenseFormatError("El flujo no tiene filas mensuales (Nº, Mes, …)")

    # Fila de totales declarados: primera fila posterior con 100 % en el primer bloque
    for r in rows[last:]:
        c = cols[starts[0]]
        if not r or (r[0] and _INT_RE.match(r[0])):
            continue
        if parse_percent(_cell(r, c.get("pct"))) is not None and _money(_cell(r, c.get("advance"))) is not None:
            for c0 in starts:
                cc = cols[c0]
                flow.totals[roles[c0]] = MonthRow(0, "Total", None, parse_percent(_cell(r, cc.get("pct"))),
                                                  _money(_cell(r, cc.get("advance"))), _money(_cell(r, cc.get("retention"))),
                                                  _money(_cell(r, cc.get("advance_return"))), _money(_cell(r, cc.get("net"))))
            break

    # Tabla de avances acumulados (EP · Mes · Proyectado · Real · Diferencia)
    acc_hdr = next((i for i in range(last, len(rows)) if norm(_cell(rows[i], 0)) == "ep"
                    and any(norm(c) == "real" for c in rows[i])), None)
    if acc_hdr is not None:
        h = [norm(c) for c in rows[acc_hdr]]
        ip, ir = next((j for j, c in enumerate(h) if c == "proyectado"), None), next((j for j, c in enumerate(h) if c == "real"), None)
        idf = next((j for j, c in enumerate(h) if c.startswith("diferencia")), None)
        for r in rows[acc_hdr + 1:]:
            if not _INT_RE.match(_cell(r, 0)):
                break
            flow.accumulated.append({"n": int(r[0]), "label": _cell(r, 1), "projected": _money(_cell(r, ip)),
                                     "real": _money(_cell(r, ir)), "difference": _money(_cell(r, idf))})
    return flow


# ---------------------------------------------------------------- análisis

def uf(v: Decimal, currency: str = "UF") -> str:
    """UF 1.234,56 (miles con punto, decimales con coma)."""
    v = Decimal(v)
    text = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if v < 0 else ''}{currency} {text}" if currency != "$" else f"{'-' if v < 0 else ''}${text}"


def _p(v: Decimal) -> str:
    return f"{(v * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}".replace(".", ",") + " %"


def _ym(p: tuple[int, int] | None, fallback: str) -> str:
    return f"{p[0]:04d}-{p[1]:02d}" if p else fallback


def _add_months(p: tuple[int, int], k: int) -> tuple[int, int]:
    idx = p[0] * 12 + (p[1] - 1) + k
    return idx // 12, idx % 12 + 1


def analyze_project_flow(flow: ProjectFlow) -> dict:  # noqa: C901 - lista lineal de comprobaciones y reglas
    cur = flow.currency
    contract = flow.contract
    real = [m for m in flow.blocks["real"] if m.advance is not None and m.n > 0]
    done = [m for m in real if m.advance and m.advance > 0]
    checks: list[dict] = []
    findings: list[dict] = []

    def add_check(label: str, declared: Decimal, computed: Decimal, tol: Decimal = _TOL) -> bool:
        ok = abs(declared - computed) <= tol
        checks.append({"kind": "flow", "label": label, "section": None, "declared": str(declared), "computed": str(computed), "ok": ok})
        return ok

    def add_finding(rule: str, sev: str, title: str, detail: str, amount: Decimal | None = None) -> None:
        findings.append({"rule": rule, "severity": sev, "title": title, "detail": detail,
                         "amount": str(amount) if amount is not None else None, "statement": flow.filename or "obra"})

    # 1) Parámetros del contrato
    if flow.advance_pct is not None and flow.advance_amount is not None:
        exp = (contract * flow.advance_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not add_check(f"Anticipo = {_p(flow.advance_pct)} del contrato", flow.advance_amount, exp):
            add_finding("FLOW_ADVANCE", "alert", "El anticipo no corresponde al porcentaje del contrato",
                        f"Se declara {uf(flow.advance_amount, cur)} y {_p(flow.advance_pct)} de {uf(contract, cur)} es {uf(exp, cur)}.",
                        abs(flow.advance_amount - exp))

    # 2) Aritmética de cada mes del flujo real: Total = Avance − Retención − Devolución de anticipo
    bad_net, bad_ret = [], []
    for m in done:
        if None in (m.net, m.advance_return):
            continue
        calc = m.advance - (m.retention or ZERO) - m.advance_return
        if abs(calc - m.net) > _TOL:
            bad_net.append((m, calc))
        if flow.advance_pct is not None and abs(m.advance * flow.advance_pct - m.advance_return) > _TOL:
            bad_ret.append(m)
    checks.append({"kind": "flow", "label": "Total = Avance − Retención − Devolución de anticipo (meses con avance real)", "section": None,
                   "declared": str(len(done) - len(bad_net)), "computed": str(len(done)), "ok": not bad_net})
    for m, calc in bad_net:
        add_finding("FLOW_NET", "alert", f"EP {m.n} ({m.label}): el total no cuadra",
                    f"Total declarado {uf(m.net, cur)}, pero Avance − Retención − Devolución da {uf(calc, cur)}.", abs(m.net - calc))
    if flow.advance_pct is not None:
        checks.append({"kind": "flow", "label": f"Devolución de anticipo = {_p(flow.advance_pct)} del avance (meses con avance real)",
                       "section": None, "declared": str(len(done) - len(bad_ret)), "computed": str(len(done)), "ok": not bad_ret})
        for m in bad_ret:
            add_finding("FLOW_ADVANCE_RETURN", "review", f"EP {m.n} ({m.label}): la devolución de anticipo no es {_p(flow.advance_pct)} del avance",
                        f"Avance {uf(m.advance, cur)} → debería devolverse {uf(m.advance * flow.advance_pct, cur)}; figura {uf(m.advance_return, cur)}.",
                        abs(m.advance * flow.advance_pct - m.advance_return))

    # 3) Totales declarados contra la suma de los meses
    for role, label in (("contract", "contrato"), ("projected", "proyectado"), ("real", "real")):
        tot, rows_ = flow.totals.get(role), flow.blocks.get(role, [])
        if not tot or tot.advance is None:
            continue
        s = sum((m.advance for m in rows_ if m.n > 0 and m.advance is not None), ZERO)
        if not add_check(f"Suma de avances del flujo {label} = total declarado", tot.advance, s, Decimal("0.5")):
            add_finding("FLOW_TOTAL", "alert", f"La suma de avances del flujo {label} no cuadra con su total",
                        f"Total declarado {uf(tot.advance, cur)}; suma de los meses {uf(s, cur)}.", abs(tot.advance - s))

    # 4) Tabla de avances acumulados contra la suma mensual
    cum_real = cum_proj = ZERO
    proj = {m.n: m for m in flow.blocks.get("projected", [])}
    mismatches = []
    for a in flow.accumulated:
        rm = next((m for m in flow.blocks["real"] if m.n == a["n"]), None)
        pm = proj.get(a["n"])
        if rm and rm.advance:
            cum_real += rm.advance
        if pm and pm.advance:
            cum_proj += pm.advance
        has_real = bool(rm and rm.advance)
        if a["real"] is not None and abs(a["real"] - cum_real) > Decimal("0.1"):
            mismatches.append((a, cum_real, has_real))
        if a["projected"] is not None and pm and abs(a["projected"] - cum_proj) > Decimal("0.1"):
            findings_label = f"EP {a['n']} ({a['label']})"
            add_finding("FLOW_ACCUMULATED", "review", f"{findings_label}: el proyectado acumulado no coincide con la suma mensual",
                        f"Tabla de acumulados {uf(a['projected'], cur)}; suma de los meses {uf(cum_proj, cur)}.", abs(a["projected"] - cum_proj))
    if flow.accumulated:
        checks.append({"kind": "flow", "label": "Acumulado real de la tabla = suma de avances reales mensuales", "section": None,
                       "declared": str(len(flow.accumulated) - len(mismatches)), "computed": str(len(flow.accumulated)), "ok": not mismatches})
    for a, c, has_real in mismatches:
        add_finding("FLOW_ACCUMULATED", "review", f"EP {a['n']} ({a['label']}): el acumulado real no coincide con la suma de los meses",
                    f"La tabla de acumulados dice {uf(a['real'], cur)} y la suma de los avances reales da {uf(c, cur)} "
                    f"(diferencia {uf(a['real'] - c, cur)})." + ("" if has_real else " Ese mes todavía no tiene avance real: revisa si es un residuo de otra versión."),
                    abs(a["real"] - c))

    # 4b) Devolución de anticipo acumulada: debe ser la suma de las devoluciones reales y nunca superar el anticipo
    if flow.advance_return_accumulated:
        run = ZERO
        prev_col: Decimal | None = None
        bad_acc = []
        for m in flow.blocks["real"]:
            if m.n == 0 or not m.advance:
                continue
            run += m.advance_return or ZERO
            col = flow.advance_return_accumulated.get(m.n)
            if col is not None and abs(col - run) > Decimal("0.1"):
                bad_acc.append((m, col, run, prev_col))
            prev_col = col if col is not None else prev_col
        checks.append({"kind": "flow", "label": "Devolución de anticipo acumulada = suma de devoluciones reales", "section": None,
                       "declared": str(len([1 for v in flow.advance_return_accumulated.values() if v is not None]) - len(bad_acc)),
                       "computed": str(len([1 for v in flow.advance_return_accumulated.values() if v is not None])), "ok": not bad_acc})
        for m, col, expected, before in bad_acc:
            over = flow.advance_amount is not None and col > flow.advance_amount
            hint = ""
            if before is not None and m.net is not None and abs((col - before) - m.net) <= _TOL:
                hint = (f" El salto del mes ({uf(col - before, cur)}) coincide con el Total pagado del mes ({uf(m.net, cur)}), "
                        f"no con la devolución ({uf(m.advance_return or ZERO, cur)}): parece una fórmula que suma la columna equivocada.")
            add_finding("FLOW_ADVANCE_ACC", "alert" if over else "review",
                        f"EP {m.n} ({m.label}): devolución de anticipo acumulada {'imposible' if over else 'distinta'}: {uf(col, cur)}",
                        f"La suma de las devoluciones reales es {uf(expected, cur)} (diferencia {uf(col - expected, cur)})."
                        + (f" Además supera el anticipo entregado ({uf(flow.advance_amount, cur)})." if over else "") + hint,
                        abs(col - expected))

    # 5) Atraso: real acumulado vs proyectado acumulado al último mes con avance real
    summary: dict = {"currency": cur, "contract": str(contract)}
    if done:
        lastm = done[-1]
        real_cum = sum((m.advance for m in done), ZERO)
        proj_cum = sum((proj[m.n].advance for m in done if m.n in proj and proj[m.n].advance is not None), ZERO)
        gap = proj_cum - real_cum
        gap_pct = gap / contract if contract else ZERO
        remaining = contract - real_cum
        summary.update({
            "last_ep": lastm.n, "last_month": lastm.label, "real_cumulative": str(real_cum), "projected_cumulative": str(proj_cum),
            "real_progress": str(real_cum / contract if contract else ZERO), "projected_progress": str(proj_cum / contract if contract else ZERO),
            "gap": str(gap), "gap_share": str(gap_pct), "remaining": str(remaining),
        })
        if gap > 0:
            sev = "alert" if gap_pct >= Decimal("0.05") else ("review" if gap_pct >= Decimal("0.02") else "info")
            add_finding("FLOW_BEHIND", sev, f"Atraso de {uf(gap, cur)} ({_p(gap_pct)} del contrato) al EP {lastm.n}",
                        f"Avance real acumulado {uf(real_cum, cur)} ({_p(real_cum / contract)}) frente a {uf(proj_cum, cur)} "
                        f"({_p(proj_cum / contract)}) del flujo proyectado actualizado a {lastm.label}.", gap)
        elif gap < 0:
            add_finding("FLOW_AHEAD", "info", f"Adelanto de {uf(-gap, cur)} sobre lo proyectado al EP {lastm.n}",
                        f"Avance real {uf(real_cum, cur)} frente a {uf(proj_cum, cur)} proyectados a {lastm.label}.", -gap)

        # Tendencia: últimos 3 EP reales contra su proyección
        recent = [m for m in done[-3:] if m.n in proj and proj[m.n].advance is not None]
        if len(recent) == 3:
            under = sum((proj[m.n].advance - m.advance for m in recent), ZERO)
            if under > 0 and all(m.advance < proj[m.n].advance for m in recent):
                add_finding("FLOW_TREND", "review", "Los últimos 3 estados de pago rinden menos de lo proyectado",
                            "; ".join(f"{m.label}: real {uf(m.advance, cur)} vs {uf(proj[m.n].advance, cur)}" for m in recent)
                            + f". El déficit de estos tres meses suma {uf(under, cur)}.", under)

        # Proyección de término al ritmo reciente (estimación)
        paces = [m.advance for m in done[-3:]]
        pace = statistics.mean(paces) if paces else ZERO
        end_planned = max((m.period for m in flow.blocks.get("contract", []) if m.advance and m.period), default=None)
        if remaining > 0 and pace > 0 and lastm.period:
            months = math.ceil(remaining / pace)
            eta = _add_months(lastm.period, months)
            late = (eta[0] * 12 + eta[1]) - (end_planned[0] * 12 + end_planned[1]) if end_planned else None
            summary.update({"recent_pace": str(pace), "eta": _ym(eta, ""), "planned_end": _ym(end_planned, "") if end_planned else None})
            if late and late > 0:
                add_finding("FLOW_FINISH", "review", f"Al ritmo reciente el término sería {_ym(eta, '')}, {late} mes(es) después de lo contratado",
                            f"Faltan {uf(remaining, cur)} ({_p(remaining / contract)} del contrato). Promedio de los últimos {len(paces)} estados "
                            f"de pago: {uf(pace, cur)} al mes → {months} mes(es) más. Término contractual: {_ym(end_planned, '')}. "
                            "Es una estimación al ritmo reciente; revisa el programa con el contratista.", remaining)
            else:
                add_finding("FLOW_FINISH", "info", f"Al ritmo reciente el término sería {_ym(eta, '')}",
                            f"Faltan {uf(remaining, cur)} ({_p(remaining / contract)} del contrato) a {uf(pace, cur)} al mes.", remaining)

    # 6) Garantías: retenciones 0 % y devolución de anticipo
    if flow.retention_pct is not None and flow.retention_pct == 0:
        add_finding("FLOW_RETENTION", "info", "El contrato no retiene garantía (0 %)",
                    "No hay retenciones sobre los estados de pago; la cobertura del anticipo depende de las boletas de garantía vigentes. "
                    "Confirma que sus vencimientos cubran hasta la recepción final.", None)
    order = {"alert": 0, "review": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], -(Decimal(f["amount"]) if f["amount"] else ZERO)))
    months = []
    for m in flow.blocks["real"]:
        c_ = next((x for x in flow.blocks.get("contract", []) if x.n == m.n), None)
        p_ = proj.get(m.n)
        months.append({"n": m.n, "label": m.label, "period": _ym(m.period, ""),
                       "contract": str(c_.advance) if c_ and c_.advance is not None else None,
                       "projected": str(p_.advance) if p_ and p_.advance is not None else None,
                       "real": str(m.advance) if m.advance is not None else None})
    summary["months"] = months
    return {"kind": "project_flow", "filename": flow.filename, "summary": summary, "checks": checks, "findings": findings}
