"""Exportaciones de facturación de nube (AWS CUR y Cost Explorer, Azure Cost Management, GCP Billing) como origen de análisis de gasto.

Detecta el formato por sus columnas, normaliza cada fila a (día, mes, servicio, región, cuenta, monto) y resume: gasto por mes, servicio, región y
cuenta; qué servicios subieron o aparecieron entre los dos últimos meses COMPLETOS; picos diarios; créditos e impuestos. Sin red y sin IA.

Lo que NO hace: no ve recursos individuales (no puede decir «esta instancia está ociosa»; para eso hay que conectar la cuenta o importar el inventario),
no suma monedas distintas (si el archivo las mezcla se rechaza) y no inventa ahorros: los hallazgos son pistas de dónde mirar.
"""
from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import NamedTuple

from .parser import ExpenseFormatError, detect_period, parse_amount

CLOUD_MAX_ROWS = 1_000_000
ZERO = Decimal(0)
Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")

# Umbrales de los hallazgos (fracciones del gasto del mes anterior o del total)
GROWTH_MIN_SHARE = Decimal("0.05")      # un servicio subió al menos el 5 % del gasto total del mes anterior…
GROWTH_MIN_PCT = Decimal("0.25")        # …y al menos 25 % respecto de sí mismo
GROWTH_ALERT_SHARE = Decimal("0.20")    # …y si subió ≥ 20 % del gasto total del mes anterior, se pone primero
NEW_SERVICE_SHARE = Decimal("0.03")
TOTAL_CHANGE_PCT = Decimal("0.10")
SPIKE_RATIO = Decimal("2.5")
SPIKE_MIN_SHARE = Decimal("0.01")
SPIKE_WINDOW = 14
SPIKE_MIN_DAYS = 14
CONCENTRATION_SHARE = Decimal("0.50")
UNCLASSIFIED_SHARE = Decimal("0.05")
MAX_TOP = 25
MAX_DAILY_POINTS = 400

_CREDIT_KINDS = {"credit", "refund", "edpdiscount", "bundleddiscount", "privaterediscount", "discount", "adjustment"}


class CloudRow(NamedTuple):
    day: date | None
    period: str          # YYYY-MM
    service: str
    region: str
    account: str
    group: str
    amount: Decimal
    kind: str            # usage | tax | credit | refund | …  (minúsculas)


@dataclass
class CloudExport:
    provider: str            # aws | azure | gcp
    fmt: str                 # cur | cost_explorer | cost_management | billing_export
    currency: str
    rows: list[CloudRow]
    notes: list[str] = field(default_factory=list)


def _k(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _pick(keys: list[str], *cands: str) -> int | None:
    for c in cands:
        if c in keys:
            return keys.index(c)
    return None


def _amount(raw: str) -> Decimal | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
        return d if d.is_finite() else None
    except InvalidOperation:
        return parse_amount(s)


def _day(raw: str) -> tuple[date | None, str | None]:
    """(día, 'YYYY-MM'); el día es None si el dato solo trae el mes. None, None si no es una fecha."""
    s = (raw or "").strip()
    if m := re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?", s):
        y, mo = int(m[1]), int(m[2])
        if 1 <= mo <= 12 and 2000 <= y <= 2100:
            if m[3]:
                try:
                    return date(y, mo, int(m[3])), f"{y:04d}-{mo:02d}"
                except ValueError:
                    return None, None
            return None, f"{y:04d}-{mo:02d}"
    if m := re.match(r"^(\d{4})(\d{2})$", s):
        y, mo = int(m[1]), int(m[2])
        if 1 <= mo <= 12 and 2000 <= y <= 2100:
            return None, f"{y:04d}-{mo:02d}"
    if m := re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        mo, d = (b, a) if a > 12 else (a, b)            # MM/DD/AAAA (Azure EA); si el primero no puede ser mes, es DD/MM
        try:
            return date(y, mo, d), f"{y:04d}-{mo:02d}"
        except ValueError:
            return None, None
    return None, None


# ------------------------------------------------------------------------------------------ detección por columnas
def _long_format(keys: list[str]) -> dict | None:
    """Mapa de columnas si el encabezado es un export «largo» (una fila por línea de factura)."""
    p = lambda *c: _pick(keys, *c)  # noqa: E731
    if any(k.startswith("lineitem") for k in keys):
        cost = p("lineitemunblendedcost", "lineitemblendedcost", "unblendedcost")
        date_ = p("lineitemusagestartdate", "usagestartdate", "billbillingperiodstartdate")
        service = p("productproductname", "productservicecode", "lineitemproductcode", "productcode")
        if cost is not None and date_ is not None and service is not None:
            return {"provider": "aws", "fmt": "cur", "cost": cost, "date": date_, "service": service,
                    "region": p("productregion", "productregioncode", "productlocation"),
                    "account": p("lineitemusageaccountid", "lineitemusageaccountname", "usageaccountid"),
                    "group": None, "currency": p("lineitemcurrencycode", "currencycode"), "kind": p("lineitemlineitemtype")}
    if "servicedescription" in keys and ("usagestarttime" in keys or "invoicemonth" in keys) and "cost" in keys:
        return {"provider": "gcp", "fmt": "billing_export", "cost": keys.index("cost"),
                "date": p("usagestarttime", "invoicemonth"), "service": keys.index("servicedescription"),
                "region": p("locationregion", "locationlocation", "locationzone"), "account": p("projectid", "projectname", "billingaccountid"),
                "group": None, "currency": p("currency"), "kind": p("costtype")}
    if "billedcost" in keys and "chargeperiodstart" in keys and "servicename" in keys:           # FOCUS (FinOps Foundation): AWS, Azure, GCP, OCI
        return {"provider": "focus", "provider_col": p("providername", "providerid"), "fmt": "focus", "cost": keys.index("billedcost"),
                "date": keys.index("chargeperiodstart"), "service": keys.index("servicename"), "region": p("regionname", "regionid"),
                "account": p("subaccountname", "subaccountid", "billingaccountname", "billingaccountid"), "group": p("servicecategory"),
                "currency": p("billingcurrency"), "kind": p("chargecategory")}
    if any(k in keys for k in ("metercategory", "costinbillingcurrency", "pretaxcost")):
        cost = p("costinbillingcurrency", "pretaxcost", "costinusd", "cost")
        date_ = p("date", "usagedatetime", "usagedate", "billingperiodstartdate")
        service = p("servicename", "metercategory", "consumedservice", "servicefamily")
        if cost is not None and date_ is not None and service is not None:
            return {"provider": "azure", "fmt": "cost_management", "cost": cost, "date": date_, "service": service,
                    "region": p("resourcelocation", "location", "resourcelocationnormalized"),
                    "account": p("subscriptionname", "subscriptionid", "accountname"),
                    "group": p("resourcegroup", "resourcegroupname"), "currency": p("billingcurrencycode", "billingcurrency", "currency"),
                    "kind": None}
    return None


# Sinónimos para tablas de facturación con otras cabeceras. Se exige fecha + un costo «de verdad» (cost/costo/charge…) + un servicio/producto;
# así «Proveedor / Concepto / Importe» de un estado de gastos NO se confunde con una factura de nube.
_SYN = {
    "date": ("date", "fecha", "day", "dia", "usagedate", "usagedatetime", "usagestartdate", "usagestarttime", "chargeperiodstart", "billingmonth",
             "invoicemonth", "startdate", "period", "periodo", "month", "mes"),
    "cost": ("cost", "costo", "costs", "costos", "netcost", "totalcost", "costototal", "charge", "charges", "cargo", "cargos", "unblendedcost",
             "billedcost", "effectivecost", "pretaxcost", "costinbillingcurrency", "costusd"),
    "service": ("service", "servicio", "servicename", "nombredelservicio", "servicedescription", "product", "producto", "productname", "metercategory",
                "sku", "skudescription", "servicecategory"),
    "region": ("region", "regionname", "regionid", "location", "locationregion", "resourcelocation", "zona", "ubicacion"),
    "account": ("account", "cuenta", "accountid", "accountname", "subscription", "suscripcion", "subscriptionid", "subscriptionname", "project",
                "proyecto", "projectid", "linkedaccount", "subaccountid", "subaccountname"),
    "group": ("resourcegroup", "grupo", "grupoderecursos", "team", "equipo", "costcenter", "centrodecosto", "area", "environment", "entorno"),
    "currency": ("currency", "moneda", "currencycode", "billingcurrency", "billingcurrencycode"),
    "kind": ("chargetype", "chargecategory", "costtype", "recordtype", "tipodecargo"),
}
_CLOUDISH = re.compile(
    r"(?i)\b(amazon|aws|azure|google|gcp|gce|cloud|compute|storage|ec2|s3|rds|ebs|vm|virtual machines?|lambda|bigquery|kubernetes|gke|eks|aks|"
    r"blob|cdn|route ?53|dynamodb|cosmos|sql database|app service|functions?|data transfer|bandwidth|vpc|nat gateway|load balancer|cloudwatch|"
    r"cloudfront|redshift|elasticache|monitor|key vault|support|marketplace)\b")
MAPPING_FIELDS = ("date", "cost", "service", "region", "account", "group", "currency", "kind")


def _generic_format(keys: list[str]) -> dict | None:
    pick = {f: _pick(keys, *cands) for f, cands in _SYN.items()}
    if pick["date"] is None or pick["cost"] is None or pick["service"] is None:
        return None
    return {"provider": "other", "fmt": "generic_billing", **pick}


def _mapped_format(header: list[str], mapping: dict[str, str]) -> dict | None:
    """Columnas elegidas a mano por el usuario ({campo: nombre de columna}). None si esta fila no es la cabecera."""
    keys = [_k(c) for c in header]
    spec: dict = {"provider": "other", "fmt": "mapped_billing", "region": None, "account": None, "group": None, "currency": None, "kind": None}
    for fld in MAPPING_FIELDS:
        wanted = mapping.get(fld)
        if not wanted:
            continue
        idx = _pick(keys, _k(wanted))
        if idx is None:
            if fld in ("date", "cost", "service"):
                return None
            raise ExpenseFormatError(f"No existe la columna «{wanted}» (campo {fld}). Columnas del archivo: {', '.join(c for c in header if c)[:300]}")
        spec[fld] = idx
    if any(spec.get(f) is None for f in ("date", "cost", "service")):
        return None
    return spec


_CE_DIMS = {"service", "region", "linkedaccount", "usagetype", "usagetypegroup", "instancetype", "recordtype", "availabilityzone",
            "apioperation", "operatingsystem", "databaseengine", "platform", "tenancy", "purchaseoption", "legalentity", "billingentity",
            "invoiceid", "resource", "costcategory", "linkedaccountname"}
_CE_COL = re.compile(r"^(?P<label>.*?)\s*\((?P<cur>[^)]{1,4})\)\s*$")
_CUR_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}


def _wide_format(header: list[str]) -> list[tuple[int, date | None, str, str]] | None:
    """Columnas de importe de un Cost Explorer «ancho»: [(índice, día, 'YYYY-MM', moneda)]. None si no lo parece."""
    keys = [_k(c) for c in header]
    if not keys or not (keys[0] in _CE_DIMS or keys[0].startswith("tag")):
        return None
    cols = []
    for i, h in enumerate(header[1:], start=1):
        m = _CE_COL.match(h.strip())
        if not m or "total" in _k(m["label"]):
            continue
        d, period = _day(m["label"].strip())
        if period is None:
            per = detect_period(m["label"])
            period = f"{per[0]:04d}-{per[1]:02d}" if per else None
        if period is None:
            continue
        cur = m["cur"].strip()
        cols.append((i, d, period, _CUR_SYMBOL.get(cur, cur.upper())))
    return cols or None


def detect_cloud(rows: list[list[str]], filename: str = "", columns: dict[str, str] | None = None) -> CloudExport | None:
    """CloudExport si el archivo es una exportación de facturación de nube; None si es otra cosa (p. ej. un estado de gastos).

    `columns` ({campo: nombre de columna}) fuerza la lectura de una tabla con cabeceras propias: date, cost y service son obligatorios.
    """
    if len(rows) > CLOUD_MAX_ROWS + 20:
        return None
    if columns:
        for hi, header in enumerate(rows[:15]):
            if (spec := _mapped_format(header, columns)) is not None:
                return _parse_long(rows[hi + 1:], spec, filename, header)
        raise ExpenseFormatError("No se encontró una fila de cabecera con las columnas indicadas (fecha, costo y servicio)")
    for hi, header in enumerate(rows[:15]):
        if len([c for c in header if c]) < 3:
            continue
        keys = [_k(c) for c in header]
        spec = _long_format(keys)
        if spec:
            return _parse_long(rows[hi + 1:], spec, filename, header)
        if generic := _generic_format(keys):
            # Por cabeceras sinónimas solo se acepta si los servicios PARECEN de nube: «Fecha / Servicio / Costo» también lo usa un edificio.
            names = [_cell(r, generic["service"]) for r in rows[hi + 1:hi + 401] if any(c.strip() for c in r)]
            if names and sum(1 for n in names if _CLOUDISH.search(n)) / len(names) >= 0.3:
                return _parse_long(rows[hi + 1:], generic, filename, header)
        wide = _wide_format(header)
        if wide:
            return _parse_wide(header, rows[hi + 1:], wide, filename)
    return None


def _cell(row: list[str], idx: int | None) -> str:
    return row[idx].strip() if idx is not None and idx < len(row) else ""


def _parse_long(rows: list[list[str]], spec: dict, filename: str, header: list[str] | None = None) -> CloudExport:
    out: list[CloudRow] = []
    notes: list[str] = []
    header = header or []
    header_names = {f: (header[spec[f]] if spec.get(f) is not None and spec[f] < len(header) else "—") for f in ("date", "cost", "service")}
    currencies: set[str] = set()
    bad_amount = bad_date = 0
    ambiguous = False
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        amount = _amount(_cell(row, spec["cost"]))
        if amount is None:
            bad_amount += 1
            continue
        day, period = _day(_cell(row, spec["date"]))
        if period is None:
            bad_date += 1
            continue
        raw_date = _cell(row, spec["date"])
        if re.match(r"^\d{1,2}/\d{1,2}/", raw_date):
            a, b = (int(x) for x in raw_date.split("/")[:2])
            ambiguous = ambiguous or (a <= 12 and b <= 12 and a != b)
        cur = _cell(row, spec["currency"]).upper()
        if cur:
            currencies.add(cur)
        out.append(CloudRow(day, period, _cell(row, spec["service"]) or "Sin servicio", _cell(row, spec["region"]) or "—",
                            _cell(row, spec["account"]) or "—", _cell(row, spec["group"]) or "—", amount,
                            re.sub(r"[^a-z]", "", _cell(row, spec["kind"]).lower()) or "usage"))
    if not out:
        raise ExpenseFormatError("No se encontraron filas con fecha y costo en la exportación de facturación")
    if bad_amount:
        notes.append(f"{bad_amount} filas sin un costo numérico se ignoraron.")
    if bad_date:
        notes.append(f"{bad_date} filas sin una fecha reconocible se ignoraron.")
    if ambiguous:
        notes.append("Las fechas vienen como día/mes ambiguos (p. ej. 03/04/2026); se interpretaron como MM/DD/AAAA.")
    if len(currencies) > 1:
        raise ExpenseFormatError(f"El archivo mezcla monedas ({', '.join(sorted(currencies))}): sepáralo por moneda para no sumarlas entre sí")
    currency = next(iter(currencies), "")
    provider = spec["provider"]
    if provider == "focus":
        names = {_cell(r, spec["provider_col"]).lower() for r in rows if _cell(r, spec["provider_col"])} if spec.get("provider_col") is not None else set()
        provider = next(iter(names)) if len(names) == 1 else ("multi" if names else "focus")
        provider = {"amazon web services": "aws", "microsoft": "azure", "google cloud": "gcp", "google": "gcp"}.get(provider, provider)
        notes.append("Formato FOCUS: se usa BilledCost (lo facturado); EffectiveCost (amortizado) no se mezcla.")
    elif spec["fmt"] in ("generic_billing", "mapped_billing"):
        how = "indicadas por ti" if spec["fmt"] == "mapped_billing" else "reconocidas por sinónimo"
        notes.append(f"Columnas {how}: fecha=«{header_names['date']}», costo=«{header_names['cost']}», servicio=«{header_names['service']}». "
                     "Si no son las correctas, indícalas a mano.")
    if not currency:
        currency = "USD" if provider == "aws" else "?"
        notes.append("El archivo no declara la moneda" + (": se asumió USD (lo habitual en AWS)." if currency == "USD" else "."))
    return CloudExport(provider, spec["fmt"], currency, out, notes)


def _parse_wide(header: list[str], rows: list[list[str]], cols: list[tuple[int, date | None, str, str]], filename: str) -> CloudExport:
    currencies = {c[3] for c in cols}
    if len(currencies) > 1:
        raise ExpenseFormatError(f"El archivo mezcla monedas ({', '.join(sorted(currencies))}): sepáralo por moneda")
    dim = header[0].strip() or "Service"
    dim_key = _k(dim)
    out: list[CloudRow] = []
    for row in rows:
        label = _cell(row, 0)
        if not label or _k(label).startswith("total"):
            continue
        for idx, day, period, _cur in cols:
            amount = _amount(_cell(row, idx))
            if amount is None:
                continue
            service = label if dim_key in ("service", "") else "Sin servicio"
            region = label if dim_key in ("region", "availabilityzone") else "—"
            account = label if dim_key.startswith("linkedaccount") else "—"
            out.append(CloudRow(day, period, service, region, account, "—", amount, "usage"))
    if not out:
        raise ExpenseFormatError("No se encontraron importes en la exportación de Cost Explorer")
    notes = []
    if dim_key not in ("service", ""):
        notes.append(f"Esta exportación agrupa por «{dim}», no por servicio: el desglose por servicio no está disponible.")
    return CloudExport("aws", "cost_explorer", next(iter(currencies)), out, notes)


# ------------------------------------------------------------------------------------------ análisis
def _fmt(v: Decimal) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _q2(v: Decimal) -> str:
    return str(v.quantize(Q2))


def _share(v: Decimal, total: Decimal) -> str:
    return str((v / total).quantize(Q4)) if total > 0 else "0"


def _finding(rule: str, severity: str, title: str, detail: str, amount: Decimal | None, period: str | None) -> dict:
    return {"rule": rule, "severity": severity, "title": title, "detail": detail,
            "amount": _q2(abs(amount)) if amount is not None else None, "statement": period}


def _complete_periods(rows: list[CloudRow], periods: list[str]) -> tuple[dict[str, bool], list[str]]:
    complete = dict.fromkeys(periods, True)
    notes: list[str] = []
    days_by_p: dict[str, set[int]] = defaultdict(set)
    for r in rows:
        if r.day:
            days_by_p[r.period].add(r.day.day)
    for i, p in enumerate(periods):
        days = days_by_p.get(p)
        if not days:
            continue
        y, m = int(p[:4]), int(p[5:])
        last = monthrange(y, m)[1]
        if (i == 0 and min(days) > 1) or (i == len(periods) - 1 and max(days) < last):
            complete[p] = False
            notes.append(f"{p} está incompleto en el archivo (días {min(days)}–{max(days)} de {last}): no se usa para comparar meses.")
    return complete, notes


def _breakdown(rows: list[CloudRow], attr: str, total: Decimal, periods: list[str], keep: int) -> list[dict]:
    agg: dict[str, Decimal] = defaultdict(lambda: ZERO)
    per: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in rows:
        key = getattr(r, attr)
        agg[key] += r.amount
        per[key][r.period] += r.amount
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = [{"name": k, "total": _q2(v), "share": _share(v, total), "months": {p: _q2(per[k][p]) for p in periods[-6:] if p in per[k]}}
             for k, v in ranked[:keep]]
    rest = ranked[keep:]
    if rest:
        rest_total = sum((v for _, v in rest), ZERO)
        shown.append({"name": f"Otros ({len(rest)})", "total": _q2(rest_total), "share": _share(rest_total, total), "months": {}})
    return shown


def analyze_cloud(exp: CloudExport, filename: str) -> dict:
    rows = exp.rows
    cur = exp.currency
    periods = sorted({r.period for r in rows})
    complete, notes = _complete_periods(rows, periods)
    notes = exp.notes + notes
    total = sum((r.amount for r in rows), ZERO)
    by_period: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        by_period[r.period] += r.amount
    daily = any(r.day for r in rows)
    findings: list[dict] = []

    credits = sum((r.amount for r in rows if r.kind in _CREDIT_KINDS and r.amount < 0), ZERO)
    tax = sum((r.amount for r in rows if r.kind == "tax"), ZERO)
    if credits < 0:
        findings.append(_finding("CLOUD_CREDITS", "info", "Hay créditos o reembolsos aplicados",
                                 f"Suman {cur} {_fmt(-credits)}. El gasto neto ({cur} {_fmt(total)}) es menor que el gasto real sin esos créditos; "
                                 "cuando se acaben, la factura sube sin que cambie el uso.", credits, None))

    # --- comparación entre los dos últimos meses completos
    done = [p for p in periods if complete[p]]
    movers: list[dict] = []
    svc_by_p: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in rows:
        svc_by_p[r.service][r.period] += r.amount
    if len(done) >= 2:
        prev_p, cur_p = done[-2], done[-1]
        tot_prev, tot_cur = by_period[prev_p], by_period[cur_p]
        for svc, per in svc_by_p.items():
            prev, now = per.get(prev_p, ZERO), per.get(cur_p, ZERO)
            if prev == 0 and now == 0:
                continue
            delta = now - prev
            movers.append({"name": svc, "previous": _q2(prev), "current": _q2(now), "delta": _q2(delta),
                           "pct": str((delta / prev).quantize(Q4)) if prev > 0 else None})
            if tot_prev > 0 and delta > 0:
                if prev > 0 and delta >= tot_prev * GROWTH_MIN_SHARE and delta / prev >= GROWTH_MIN_PCT:
                    sev = "alert" if delta >= tot_prev * GROWTH_ALERT_SHARE else "review"
                    findings.append(_finding(
                        "CLOUD_SPEND_GROWTH", sev, f"{svc} subió {_fmt((delta / prev * 100).quantize(Decimal('0.1')))} %",
                        f"De {cur} {_fmt(prev)} en {prev_p} a {cur} {_fmt(now)} en {cur_p} (+{cur} {_fmt(delta)}). "
                        "Comprueba si fue un despliegue, un cambio de uso o un recurso olvidado.", delta, cur_p))
                elif prev == 0 and now >= tot_cur * NEW_SERVICE_SHARE:
                    findings.append(_finding("CLOUD_NEW_SERVICE", "review", f"{svc} aparece en {cur_p}",
                                             f"No tenía gasto en {prev_p} y en {cur_p} suma {cur} {_fmt(now)} "
                                             f"({_fmt((now / tot_cur * 100).quantize(Decimal('0.1')))} % del mes).", now, cur_p))
        movers.sort(key=lambda m: (-abs(Decimal(m["delta"])), m["name"]))
        movers = movers[:15]
        if tot_prev > 0:
            change = (tot_cur - tot_prev) / tot_prev
            if abs(change) >= TOTAL_CHANGE_PCT:
                word = "subió" if change > 0 else "bajó"
                findings.append(_finding("CLOUD_TOTAL_CHANGE", "info", f"El gasto total {word} {_fmt((abs(change) * 100).quantize(Decimal('0.1')))} %",
                                         f"{cur} {_fmt(tot_prev)} en {prev_p} → {cur} {_fmt(tot_cur)} en {cur_p}.", tot_cur - tot_prev, cur_p))
    elif len(periods) >= 2:
        notes.append("Hay menos de dos meses completos: no se puede comparar un mes con otro.")
    elif len(periods) == 1:
        notes.append("El archivo trae un solo mes: sube varios meses para ver qué creció.")

    # --- picos diarios por servicio
    anomalies: list[dict] = []
    if daily:
        series: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
        for r in rows:
            if r.day:
                series[r.service][r.day] += r.amount
        floor = max(Decimal(1), abs(total) * SPIKE_MIN_SHARE) if total else Decimal(1)
        for svc, per in series.items():
            days = sorted(per)
            if len(days) < SPIKE_MIN_DAYS:
                continue
            for i, d in enumerate(days):
                window = [per[x] for x in days[max(0, i - SPIKE_WINDOW):i]]
                if len(window) < 7:
                    continue
                base = Decimal(str(median(window)))
                v = per[d]
                if base > 0 and v >= base * SPIKE_RATIO and v - base >= floor:
                    anomalies.append({"name": svc, "day": d.isoformat(), "amount": _q2(v), "baseline": _q2(base), "excess": _q2(v - base)})
        anomalies.sort(key=lambda a: (-Decimal(a["excess"]), a["name"], a["day"]))
        anomalies = anomalies[:10]
        for a in anomalies[:5]:
            findings.append(_finding("CLOUD_DAILY_SPIKE", "review", f"Pico de {a['name']} el {a['day']}",
                                     f"Ese día costó {cur} {_fmt(Decimal(a['amount']))}, frente a una mediana de {cur} {_fmt(Decimal(a['baseline']))} "
                                     "de los días anteriores. Puede ser un trabajo puntual, una fuga de datos de salida o un recurso descontrolado.",
                                     Decimal(a["excess"]), a["day"][:7]))
    else:
        notes.append("El archivo es mensual (sin días): no se pueden detectar picos diarios.")

    by_service = _breakdown(rows, "service", total, periods, MAX_TOP)
    if by_service and total > 0:
        top = max(svc_by_p.items(), key=lambda kv: sum(kv[1].values(), ZERO))
        top_total = sum(top[1].values(), ZERO)
        if top_total / total >= CONCENTRATION_SHARE:
            findings.append(_finding("CLOUD_CONCENTRATION", "info", f"{top[0]} concentra el gasto",
                                     f"Es el {_fmt((top_total / total * 100).quantize(Decimal('0.1')))} % del total ({cur} {_fmt(top_total)}): "
                                     "es donde más rinde optimizar.", top_total, None))
    unclassified = svc_by_p.get("Sin servicio", {})
    un_total = sum(unclassified.values(), ZERO) if unclassified else ZERO
    if total > 0 and un_total / total >= UNCLASSIFIED_SHARE:
        findings.append(_finding("CLOUD_UNCLASSIFIED", "review", "Gasto sin servicio asignado",
                                 f"{cur} {_fmt(un_total)} ({_fmt((un_total / total * 100).quantize(Decimal('0.1')))} %) no trae servicio; "
                                 "no se puede atribuir.", un_total, None))
    negative_services = [s for s, per in svc_by_p.items() if sum(per.values(), ZERO) < 0]
    if negative_services:
        findings.append(_finding("CLOUD_NEGATIVE_SERVICE", "review", "Servicios con gasto neto negativo",
                                 "Por créditos, reembolsos o ajustes: " + ", ".join(sorted(negative_services)[:5]) + ".", None, None))

    order = {"alert": 0, "review": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], -Decimal(f["amount"] or 0), f["rule"], f["title"]))
    day_values = [r.day for r in rows if r.day]
    day_totals: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        if r.day:
            day_totals[r.day] += r.amount
    return {
        "filename": filename, "provider": exp.provider, "format": exp.fmt, "currency": cur, "rows": len(rows),
        "granularity": "daily" if daily else "monthly",
        "first_day": min(day_values).isoformat() if day_values else (periods[0] if periods else None),
        "last_day": max(day_values).isoformat() if day_values else (periods[-1] if periods else None),
        "total": _q2(total), "credits": _q2(credits), "tax": _q2(tax),
        "months": [{"period": p, "total": _q2(by_period[p]), "complete": complete[p]} for p in periods],
        "by_service": by_service,
        "by_region": _breakdown(rows, "region", total, periods, 15) if any(r.region != "—" for r in rows) else [],
        "by_account": _breakdown(rows, "account", total, periods, 15) if any(r.account != "—" for r in rows) else [],
        "by_group": _breakdown(rows, "group", total, periods, 15) if any(r.group != "—" for r in rows) else [],
        "daily": [{"day": d.isoformat(), "amount": _q2(v)} for d, v in sorted(day_totals.items())[-MAX_DAILY_POINTS:]],
        "movers": movers, "anomalies": anomalies, "findings": findings, "notes": notes,
        "compared": [done[-2], done[-1]] if len(done) >= 2 else None,
    }
