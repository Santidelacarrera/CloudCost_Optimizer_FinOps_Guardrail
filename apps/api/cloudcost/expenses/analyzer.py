"""Reglas deterministas de revisión de gastos. Sin IA, sin red: mismo archivo → mismo resultado.

Cada hallazgo es una *pista para revisar*, no una conclusión: que un gasto sea o no innecesario depende de contexto
que el archivo no trae. Por eso se informa el monto involucrado (no un «ahorro»), el motivo y qué verificar.

Severidades: alert (probable error, revisar primero) · review (verificar) · info (contexto).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .parser import Item, Statement, norm

ZERO = Decimal(0)
_STOP = {"del", "los", "las", "para", "por", "con", "sin", "servicio", "servicios", "mantencion", "mantenimiento",
         "pago", "gasto", "gastos", "cuota", "articulos"}


@dataclass(frozen=True)
class ExpenseConfig:
    tolerance: Decimal = Decimal(1)                  # diferencia aceptada en cuadraturas (redondeos)
    section_share_info: Decimal = Decimal("0.50")    # una sección con ≥50 % del total se reporta como concentración
    dominant_item_share: Decimal = Decimal("0.80")   # una partida con ≥80 % de su sección (≥3 partidas)
    top_n: int = 3
    change_pct: Decimal = Decimal("0.15")            # variación mensual relevante: ≥15 %…
    change_min_share: Decimal = Decimal("0.01")      # …y ≥1 % del total del mes anterior
    max_findings: int = 100


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    amount: Decimal | None = None
    statement: str | None = None     # período (YYYY-MM) o nombre de archivo

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "title": self.title, "detail": self.detail,
                "amount": str(self.amount) if self.amount is not None else None, "statement": self.statement}


def clp(v: Decimal) -> str:
    """$1.234.567 (sin decimales si es entero)."""
    v = Decimal(v)
    neg = v < 0
    q = abs(v)
    text = f"{q:,.0f}" if q == q.to_integral_value() else f"{q:,.2f}"
    text = text.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-${text}" if neg else f"${text}"


def pct(share: Decimal) -> str:
    return f"{(share * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}".replace(".", ",") + " %"


def _label(st: Statement) -> str:
    return st.period_key or st.filename or "archivo"


def _total(items: list[Item]) -> Decimal:
    return sum((i.amount for i in items), ZERO)


def _tokens(label: str) -> frozenset[str]:
    return frozenset(t for t in re.split(r"[^a-z0-9]+", norm(label)) if len(t) >= 3 and t not in _STOP)


def reconcile(st: Statement, cfg: ExpenseConfig) -> tuple[list[dict], list[Finding]]:
    """Comprueba subtotales y totales declarados contra la suma de las partidas."""
    checks: list[dict] = []
    findings: list[Finding] = []
    last_sub_row: dict[str, int] = {}
    for t in sorted(st.totals, key=lambda x: x.row):
        if t.kind == "subtotal" and t.section:
            lo = last_sub_row.get(t.section, 0)
            computed = _total([i for i in st.items if i.section == t.section and lo < i.row < t.row])
            last_sub_row[t.section] = t.row
            scope = f"sección «{t.section}»"
        else:
            computed = _total([i for i in st.items if i.row < t.row])
            scope = "todas las partidas anteriores"
        ok = abs(computed - t.amount) <= cfg.tolerance
        checks.append({"kind": t.kind, "label": t.label, "section": t.section, "declared": str(t.amount),
                       "computed": str(computed), "ok": ok})
        if not ok:
            diff = t.amount - computed
            findings.append(Finding(
                "RECONCILIATION", "alert",
                f"No cuadra «{t.label}»" + (f" de {t.section}" if t.section else ""),
                f"El archivo declara {clp(t.amount)}, pero la suma de {scope} es {clp(computed)} "
                f"(diferencia {clp(diff)}). Revisa si falta o sobra una partida, o si hay un error de digitación.",
                abs(diff), _label(st)))
    return checks, findings


def analyze_statement(st: Statement, cfg: ExpenseConfig) -> tuple[dict, list[Finding]]:
    label = _label(st)
    total = _total(st.items)
    checks, findings = reconcile(st, cfg)

    by_section: dict[str, list[Item]] = defaultdict(list)
    for it in st.items:
        by_section[it.section].append(it)

    def share(v: Decimal) -> Decimal:
        return (v / total) if total else ZERO

    sections = []
    for name, items in by_section.items():
        s_total = _total(items)
        sections.append({
            "name": name, "total": str(s_total), "share": str(share(s_total)), "item_count": len(items),
            "items": [{"label": i.label, "amount": str(i.amount), "share": str(share(i.amount)), "row": i.row}
                      for i in sorted(items, key=lambda i: i.amount, reverse=True)],
        })
    sections.sort(key=lambda s: Decimal(s["total"]), reverse=True)

    # Cobros repetidos: mismo concepto más de una vez en el mismo período
    groups: dict[str, list[Item]] = defaultdict(list)
    for it in st.items:
        groups[norm(it.label)].append(it)
    for items in groups.values():
        if len(items) < 2:
            continue
        amounts = [i.amount for i in items]
        name = items[0].label.strip()
        if len(set(amounts)) < len(amounts):
            dup = max(a for a in amounts if amounts.count(a) > 1)
            findings.append(Finding(
                "DUPLICATE_CHARGE", "alert", f"Cobro idéntico repetido: «{name}»",
                f"«{name}» aparece {len(items)} veces y al menos dos con el mismo monto ({clp(dup)}). "
                "Si no son dos servicios distintos, es un cobro duplicado que se puede reclamar.", dup, label))
        else:
            findings.append(Finding(
                "REPEATED_ITEM", "review", f"«{name}» aparece {len(items)} veces",
                f"Montos: {', '.join(clp(a) for a in amounts)} (suma {clp(sum(amounts, ZERO))}). "
                "Confirma que corresponden a cuentas o medidores distintos y no a un cobro duplicado; "
                "si es una sola cuenta, pide que se facture en una línea.", sum(amounts, ZERO), label))

    # Posible mismo beneficiario o concepto pagado en secciones distintas (p. ej. sueldo y «mantención … <nombre>»)
    seen: set[tuple[int, int]] = set()
    for a in st.items:
        ta = _tokens(a.label)
        if len(ta) < 2:
            continue
        for b in st.items:
            if a.row == b.row or a.section == b.section or norm(a.label) == norm(b.label):
                continue
            tb = _tokens(b.label)
            if ta < tb and (key := (a.row, b.row)) not in seen:
                seen.add(key)
                findings.append(Finding(
                    "SAME_PAYEE_TWO_SECTIONS", "review", f"Posible mismo beneficiario en dos secciones: «{a.label.strip()}»",
                    f"«{a.label.strip()}» ({a.section}, {clp(a.amount)}) y «{' '.join(b.label.split())}» ({b.section}, {clp(b.amount)}) "
                    "parecen referirse a la misma persona o proveedor. Puede ser legítimo (dos servicios distintos); "
                    "verifica que no se esté pagando dos veces lo mismo y que haya contrato o boleta para cada concepto.",
                    a.amount + b.amount, label))

    # Concentración
    top_sec = sections[0] if sections else None
    if top_sec and len(sections) > 1 and Decimal(top_sec["share"]) >= cfg.section_share_info:
        findings.append(Finding(
            "CONCENTRATION", "info", f"«{top_sec['name']}» concentra {pct(Decimal(top_sec['share']))} del gasto",
            f"{clp(Decimal(top_sec['total']))} de {clp(total)}. Cualquier ahorro relevante pasa por esta sección; "
            "empieza por revisar sus partidas más grandes.", Decimal(top_sec["total"]), label))
    for s in sections:
        its, s_total = s["items"], Decimal(s["total"])
        if len(its) >= 3 and s_total > 0 and Decimal(its[0]["amount"]) / s_total >= cfg.dominant_item_share:
            findings.append(Finding(
                "DOMINANT_ITEM", "info", f"«{its[0]['label'].strip()}» domina «{s['name']}»",
                f"Es {pct(Decimal(its[0]['amount']) / s_total)} de la sección ({clp(Decimal(its[0]['amount']))}). "
                "Es la partida donde un cambio (tarifa, consumo, proveedor) tiene más efecto.", Decimal(its[0]["amount"]), label))
    ranked = sorted(st.items, key=lambda i: i.amount, reverse=True)
    if len(ranked) >= 5:
        top = ranked[: cfg.top_n]
        s = _total(top)
        findings.append(Finding(
            "TOP_ITEMS", "info", f"Las {cfg.top_n} mayores partidas suman {pct(share(s))} del total",
            "; ".join(f"{i.label.strip()} {clp(i.amount)}" for i in top) + ".", s, label))

    if neg := [i for i in st.items if i.amount < 0]:
        findings.append(Finding(
            "NEGATIVE_AMOUNTS", "info", f"Hay {len(neg)} monto(s) negativo(s)",
            "Descuentos, abonos o notas de crédito: " + "; ".join(f"{i.label.strip()} {clp(i.amount)}" for i in neg[:5]) + ".",
            _total(neg), label))

    summary = {
        "filename": st.filename, "period": st.period_key, "title": st.title, "total": str(total),
        "item_count": len(st.items), "sections": sections, "checks": checks, "warnings": st.warnings,
    }
    return summary, findings


def _by_label(st: Statement) -> dict[str, tuple[str, Decimal]]:
    out: dict[str, tuple[str, Decimal]] = {}
    for it in st.items:
        k = norm(it.label)
        out[k] = (out[k][0] if k in out else " ".join(it.label.split()), (out[k][1] if k in out else ZERO) + it.amount)
    return out


def compare(statements: list[Statement], cfg: ExpenseConfig) -> tuple[dict | None, list[Finding]]:
    """Compara períodos consecutivos: variación del total, partidas con alza/baja relevante, nuevas y desaparecidas."""
    if len(statements) < 2:
        return None, []
    findings: list[Finding] = []
    maps = [_by_label(s) for s in statements]
    labels = [_label(s) for s in statements]
    totals = [_total(s.items) for s in statements]
    for n in range(1, len(statements)):
        prev, cur, p_lbl, c_lbl = maps[n - 1], maps[n], labels[n - 1], labels[n]
        p_tot, c_tot = totals[n - 1], totals[n]
        floor = abs(p_tot) * cfg.change_min_share
        if p_tot:
            delta = c_tot - p_tot
            ch = delta / p_tot
            if abs(ch) >= cfg.change_pct:
                findings.append(Finding(
                    "TOTAL_CHANGE", "review", f"El total {'sube' if delta > 0 else 'baja'} {pct(abs(ch))} vs {p_lbl}",
                    f"{clp(p_tot)} → {clp(c_tot)} ({'+' if delta > 0 else '-'}{clp(abs(delta))}).", abs(delta), c_lbl))
        for k, (name, amt) in cur.items():
            if k in prev:
                before = prev[k][1]
                delta = amt - before
                if before and abs(delta / before) >= cfg.change_pct and abs(delta) >= floor:
                    up = delta > 0
                    findings.append(Finding(
                        "ITEM_CHANGE", "review" if up else "info", f"«{name}» {'sube' if up else 'baja'} {pct(abs(delta / before))} vs {p_lbl}",
                        f"{clp(before)} → {clp(amt)} ({clp(delta)}). "
                        + ("Pide el detalle de la factura y compara consumo y tarifa." if up else "Confirma que no falte un cobro del período."),
                        abs(delta), c_lbl))
            elif abs(amt) >= floor:
                findings.append(Finding("NEW_ITEM", "review", f"Partida nueva: «{name}»",
                                        f"No estaba en {p_lbl} y ahora cuesta {clp(amt)}. Verifica que esté autorizada.", abs(amt), c_lbl))
        for k, (name, amt) in prev.items():
            if k not in cur and abs(amt) >= floor:
                findings.append(Finding("DROPPED_ITEM", "info", f"Partida que desaparece: «{name}»",
                                        f"Estaba en {p_lbl} por {clp(amt)} y ya no aparece en {c_lbl}. "
                                        "Si era un gasto fijo, confirma que no quedó pendiente de cobro.", abs(amt), c_lbl))
    all_keys: dict[str, str] = {}
    for m in maps:
        for k, (name, _) in m.items():
            all_keys.setdefault(k, name)
    rows = [{"label": name, "amounts": [str(m[k][1]) if k in m else None for m in maps]} for k, name in all_keys.items()]
    rows.sort(key=lambda r: max((Decimal(a) for a in r["amounts"] if a is not None), default=ZERO), reverse=True)
    return {"periods": labels, "totals": [str(t) for t in totals], "rows": rows[:200]}, findings


_ORDER = {"alert": 0, "review": 1, "info": 2}


def analyze(statements: list[Statement], cfg: ExpenseConfig | None = None) -> dict:
    cfg = cfg or ExpenseConfig()
    # Períodos en orden cronológico; los archivos sin período se dejan en el orden en que llegaron
    ordered = sorted(statements, key=lambda s: (s.period is None, s.period or (0, 0)))
    summaries, findings = [], []
    periods = [s.period_key for s in ordered if s.period_key]
    for dup in {p for p in periods if periods.count(p) > 1}:
        findings.append(Finding("DUPLICATE_PERIOD", "alert", f"Dos archivos del mismo período ({dup})",
                                "Si son el mismo mes subido dos veces, la comparación mensual no es válida; sube cada mes una sola vez.", None, dup))
    for st in ordered:
        summary, f = analyze_statement(st, cfg)
        summaries.append(summary)
        findings.extend(f)
    comparison, cf = compare(ordered, cfg)
    findings.extend(cf)
    findings.sort(key=lambda f: (_ORDER[f.severity], -(f.amount or ZERO)))
    checks = [c for s in summaries for c in s["checks"]]
    return {
        "statements": summaries,
        "comparison": comparison,
        "findings": [f.as_dict() for f in findings[: cfg.max_findings]],
        "total_findings": len(findings),
        "checks": {"passed": sum(1 for c in checks if c["ok"]), "failed": sum(1 for c in checks if not c["ok"])},
    }
