"""Lector tolerante de estados de gastos en CSV (gastos comunes, presupuestos, listados de costos).

Entiende dos formas de archivo, sin configuración:
  * tabular: una fila por partida con columnas tipo descripción / monto (/ categoría / mes);
  * informe: secciones en mayúsculas, partidas debajo, filas «Sub-Total» y «Total» (típico de gastos comunes).

Reglas del lector: separador `;` `,` tab o `|` autodetectado; montos en formato chileno ($1.234.567) o inglés (1,234.56);
los subtotales y totales declarados NO se cuentan como gasto: se guardan aparte para poder comprobar que cuadran.
El contenido nunca se interpreta como código ni se ejecuta; todo es texto.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_ROWS = 5000
UNSECTIONED = "Otros"

MONTHS = {
    "enero": 1, "ene": 1, "febrero": 2, "feb": 2, "marzo": 3, "mar": 3, "abril": 4, "abr": 4, "mayo": 5, "may": 5,
    "junio": 6, "jun": 6, "julio": 7, "jul": 7, "agosto": 8, "ago": 8, "septiembre": 9, "setiembre": 9, "sept": 9,
    "sep": 9, "octubre": 10, "oct": 10, "noviembre": 11, "nov": 11, "diciembre": 12, "dic": 12,
}
_MONTH_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")[\s._/-]*(?:de[\s._/-]*)?(\d{4}|\d{2})(?!\d)")
_NUM_MY_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[/-](20\d{2})(?!\d)")
_NUM_YM_RE = re.compile(r"(?<!\d)(20\d{2})[/-](0?[1-9]|1[0-2])(?!\d)")
_DMY_RE = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[/.-](0?[1-9]|1[0-2])[/.-](20\d{2})(?!\d)")

_AMOUNT_RE = re.compile(
    r"^(?P<open>\()?\s*(?P<s1>[-−])?\s*(?:\$|clp|usd|us\$|€)?\s*(?P<s2>[-−])?\s*"
    r"(?P<num>\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*\)?$", re.I)

_DESC_HEADERS = re.compile(r"\b(descripcion|concepto|detalle|glosa|item|partida|gasto|description|concept|name|nombre|proveedor)\b")
_AMOUNT_HEADERS = re.compile(r"\b(monto|importe|valor|amount|costo|cost|precio|cargo|total)\b")
_CATEGORY_HEADERS = re.compile(r"\b(categoria|rubro|tipo|seccion|category|grupo|clasificacion)\b")
_PERIOD_HEADERS = re.compile(r"\b(mes|periodo|fecha|date|month|period)\b")
_TOTAL_LABEL = re.compile(r"^(sub\s*-?\s*total|total(es)?\b|suma\b)")


class ExpenseFormatError(ValueError):
    """El archivo no se puede interpretar como estado de gastos."""


@dataclass(frozen=True)
class Item:
    section: str
    label: str
    amount: Decimal
    row: int


@dataclass(frozen=True)
class DeclaredTotal:
    kind: str                 # subtotal | total
    label: str
    amount: Decimal
    section: str | None
    row: int


@dataclass
class Statement:
    filename: str
    period: tuple[int, int] | None
    title: str | None
    items: list[Item] = field(default_factory=list)
    totals: list[DeclaredTotal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def period_key(self) -> str | None:
        return f"{self.period[0]:04d}-{self.period[1]:02d}" if self.period else None


def norm(text: str) -> str:
    """Minúsculas, sin tildes y con espacios colapsados: clave de comparación de textos."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s).strip()


def parse_amount(raw: str | None) -> Decimal | None:
    """Convierte «$1.016.000», «(1.234,50)», «-3.920» o «1,234.56» en Decimal; None si no es un monto."""
    if raw is None:
        return None
    m = _AMOUNT_RE.match(raw.strip())
    if not m:
        return None
    num = re.sub(r"\s", "", m["num"])
    has_dot, has_comma = "." in num, "," in num
    if has_dot and has_comma:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        num = num.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = num.split(sep)
        thousands = len(parts) > 2 or (len(parts[1]) == 3 and parts[0] not in ("0", "00"))
        num = num.replace(sep, "") if thousands else num.replace(sep, ".")
    try:
        value = Decimal(num)
    except InvalidOperation:
        return None
    negative = bool(m["s1"] or m["s2"] or (m["open"] and raw.strip().endswith(")")))
    return -value if negative else value


def detect_period(text: str) -> tuple[int, int] | None:
    """Busca un mes y año en un texto: «AGOSTO 2025», «ago-25», «08/2025», «2025-08», «15/08/2025»."""
    t = norm(text)
    if m := _MONTH_RE.search(t):
        year = int(m[2])
        return (2000 + year if year < 100 else year, MONTHS[m[1]])
    if m := _DMY_RE.search(t):
        return int(m[3]), int(m[2])
    if m := _NUM_YM_RE.search(t):
        return int(m[1]), int(m[2])
    if m := _NUM_MY_RE.search(t):
        return int(m[2]), int(m[1])
    return None


def _detect_delimiter(text: str) -> str:
    sample = text.splitlines()[:40]
    best, best_score = ";", -1
    for d in (";", ",", "\t", "|"):
        try:
            score = sum(max(len(r) - 1, 0) for r in csv.reader(sample, delimiter=d))
        except csv.Error:
            continue
        if score > best_score:
            best, best_score = d, score
    return best


def _header_columns(cells: list[str]) -> dict[str, int] | None:
    cols: dict[str, int] = {}
    for i, c in enumerate(cells):
        h = norm(c)
        if not h or parse_amount(c) is not None:
            continue
        for key, rx in (("desc", _DESC_HEADERS), ("amount", _AMOUNT_HEADERS), ("category", _CATEGORY_HEADERS), ("period", _PERIOD_HEADERS)):
            if key not in cols and rx.search(h):
                cols[key] = i
                break
    return cols if "desc" in cols and "amount" in cols else None


def parse_statements(text: str, filename: str = "") -> list[Statement]:
    """Devuelve un estado por período (varios solo si el archivo trae una columna de mes con valores distintos)."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise ExpenseFormatError("El archivo está vacío")
    if text.count("\n") > MAX_ROWS + 200:
        raise ExpenseFormatError(f"El archivo supera el máximo de {MAX_ROWS} filas")
    try:
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=_detect_delimiter(text))]
    except csv.Error as exc:
        raise ExpenseFormatError(f"No se pudo leer el CSV: {exc}") from exc

    # 1) ¿hay fila de encabezado (descripción + monto)? Si la hay, solo se leen las filas posteriores.
    header_idx, cols = None, None
    for i, r in enumerate(rows[:30]):
        found = _header_columns(r)
        if found:
            header_idx, cols = i, found
            break
    start = (header_idx + 1) if header_idx is not None else 0

    # 2) Clasificar cada fila: partida/total (tiene monto y etiqueta), texto suelto (candidato a sección) u otra.
    kinds: list[tuple[str, str, Decimal | None]] = []   # (tipo, etiqueta, monto)
    title_cells: list[str] = []
    for i, r in enumerate(rows):
        if i < start:
            title_cells.extend(c for c in r if c)
            kinds.append(("skip", "", None))
            continue
        non_empty = [(j, c) for j, c in enumerate(r) if c]
        if not non_empty:
            kinds.append(("blank", "", None))
            continue
        amount, amount_idx = None, None
        if cols:
            amount = parse_amount(r[cols["amount"]]) if cols["amount"] < len(r) else None
            amount_idx = cols["amount"] if amount is not None else None
        else:
            j, c = non_empty[-1]
            amount = parse_amount(c)
            amount_idx = j if amount is not None else None
        label = ""
        if cols and cols["desc"] < len(r) and r[cols["desc"]]:
            label = r[cols["desc"]]
        else:
            label = next((c for j, c in non_empty if j != amount_idx and parse_amount(c) is None), "")
        if amount is not None and label:
            kinds.append(("item", label, amount))
        elif amount is None and len(non_empty) == 1 and parse_amount(non_empty[0][1]) is None:
            kinds.append(("text", non_empty[0][1], None))
        else:
            kinds.append(("noLabel" if amount is not None else "other", "", amount))

    if not any(k[0] == "item" for k in kinds):
        raise ExpenseFormatError("No se encontraron filas con descripción y monto (por ejemplo «Ascensores;$280.221»)")
    if sum(1 for k in kinds if k[0] == "item") > MAX_ROWS:
        raise ExpenseFormatError(f"El archivo supera el máximo de {MAX_ROWS} partidas")

    # 3) Recorrer en orden asignando secciones. Un texto suelto es sección solo si la siguiente fila con contenido es partida.
    def next_kind(idx: int) -> str:
        for k in kinds[idx + 1:]:
            if k[0] not in ("blank", "skip"):
                return k[0]
        return ""

    buckets: dict[tuple[int, int] | None, Statement] = {}
    title = next((c for c in title_cells if c), None)
    file_period = detect_period(" ".join(title_cells[:12])) or detect_period(filename)
    section: str | None = None
    order: list[tuple[int, int] | None] = []
    for i, (kind, label, amount) in enumerate(kinds):
        row_no = i + 1
        if kind == "text":
            if next_kind(i) == "item":
                section = label
            continue
        if kind == "noLabel":
            _warn(buckets, order, file_period, filename, title, f"Fila {row_no}: hay un monto sin descripción y se ignoró")
            continue
        if kind != "item":
            continue
        assert amount is not None
        row = rows[i]
        period = file_period
        if cols and "period" in cols and cols["period"] < len(row) and row[cols["period"]]:
            period = detect_period(row[cols["period"]]) or file_period
        st = _bucket(buckets, order, period, filename, title)
        sec = section
        if cols and "category" in cols and cols["category"] < len(row) and row[cols["category"]]:
            sec = row[cols["category"]]
        m = _TOTAL_LABEL.match(norm(label))
        if m:
            is_sub = norm(label).startswith("sub")
            st.totals.append(DeclaredTotal("subtotal" if is_sub else "total", label, amount, section if is_sub else None, row_no))
            if not is_sub:
                section = None
            continue
        st.items.append(Item(sec or UNSECTIONED, label, amount, row_no))

    return [buckets[p] for p in order if buckets[p].items]


def _bucket(buckets: dict, order: list, period, filename: str, title: str | None) -> Statement:
    if period not in buckets:
        buckets[period] = Statement(filename=filename, period=period, title=title)
        order.append(period)
    return buckets[period]


def _warn(buckets: dict, order: list, period, filename: str, title: str | None, msg: str) -> None:
    _bucket(buckets, order, period, filename, title).warnings.append(msg)
