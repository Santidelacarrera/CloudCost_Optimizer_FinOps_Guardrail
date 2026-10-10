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
    # inglés (exportaciones de nube): solo los que no coinciden con el español
    "january": 1, "jan": 1, "february": 2, "march": 3, "april": 4, "apr": 4, "june": 6, "july": 7, "august": 8, "aug": 8,
    "september": 9, "october": 10, "november": 11, "december": 12, "dec": 12,
}
_MONTH_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")[\s._/-]*(?:de[\s._/-]*)?(\d{4}|\d{2})(?!\d)")
_NUM_MY_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[/-](20\d{2})(?!\d)")
_NUM_YM_RE = re.compile(r"(?<!\d)(20\d{2})[/-](0?[1-9]|1[0-2])(?!\d)")
_DMY_RE = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[/.-](0?[1-9]|1[0-2])[/.-](20\d{2})(?!\d)")

_AMOUNT_RE = re.compile(
    r"^(?P<open>\()?\s*(?P<s1>[-−])?\s*(?:\$|clp|usd|us\$|€|uf)?\s*(?P<s2>[-−])?\s*"
    r"(?P<num>\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*\)?$", re.I)

_DESC_HEADERS = re.compile(r"\b(descripcion|concepto|detalle|glosa|item|partida|gasto|description|concept|name|nombre|proveedor)\b")
_AMOUNT_HEADERS = re.compile(r"\b(monto|importe|valor|amount|costo|cost|precio|cargo|total)\b")
_CATEGORY_HEADERS = re.compile(r"\b(categoria|rubro|tipo|seccion|category|grupo|clasificacion)\b")
_PERIOD_HEADERS = re.compile(r"\b(mes|periodo|fecha|date|month|period)\b")
_CODE_RE = re.compile(r"^\d+(?:\.\d+)*\.?$")
_NAME_HDR = re.compile(r"\b(nombre|proveedor|razon social)\b")
_DOC_HDR = re.compile(r"\b(documento|factura|folio|boleta|doc)\b")
_DATE_HDR = re.compile(r"\b(fecha)\b")
_NOTE_HDR = re.compile(r"\b(descripcion|glosa|concepto|detalle)\b")
_TOTAL_LABEL = re.compile(r"^(sub\s*-?\s*total|total(es)?\b|suma\b)")


class ExpenseFormatError(ValueError):
    """El archivo no se puede interpretar como estado de gastos."""


@dataclass(frozen=True)
class Item:
    section: str
    label: str
    amount: Decimal
    row: int
    group: str | None = None      # subgrupo (nivel 2 en informes numerados, p. ej. «Agua»)
    doc: str | None = None        # N° de documento / factura
    date: str | None = None
    note: str | None = None       # descripción del servicio


@dataclass(frozen=True)
class DeclaredTotal:
    kind: str                 # subtotal | total | group (resumen jerárquico)
    label: str
    amount: Decimal
    section: str | None
    row: int
    expected: Decimal | None = None   # kind=group: suma declarada de su detalle directo


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


def read_rows(text: str, max_rows: int = MAX_ROWS) -> list[list[str]]:
    """Lee el CSV (separador autodetectado) como filas de celdas sin espacios sobrantes."""
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise ExpenseFormatError("El archivo está vacío")
    if text.count("\n") > max_rows + 200:
        raise ExpenseFormatError(f"El archivo supera el máximo de {max_rows} filas")
    try:
        return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=_detect_delimiter(text))]
    except csv.Error as exc:
        raise ExpenseFormatError(f"No se pudo leer el CSV: {exc}") from exc


def parse_statements(text: str, filename: str = "") -> list[Statement]:
    """Devuelve un estado por período (varios solo si el archivo trae una columna de mes con valores distintos)."""
    rows = read_rows(text)
    if (hier := _parse_hierarchical(rows, filename)) is not None:
        return hier

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


def _code(cell: str) -> tuple[int, ...] | None:
    c = (cell or "").strip()
    if not c or not _CODE_RE.match(c):
        return None
    try:
        return tuple(int(x) for x in c.strip(".").split("."))
    except ValueError:
        return None


def _parse_hierarchical(rows: list[list[str]], filename: str) -> list[Statement] | None:
    """Informes con códigos jerárquicos (1. / 1.1. / 1.1.1.): las hojas son las partidas y cada padre es un resumen declarado.

    Devuelve None si el archivo no tiene esa estructura (así se evita tomar un monto como «1.016» por un código).
    """
    coded = [(i, c) for i, r in enumerate(rows) if r and (c := _code(r[0])) is not None]
    codes = {c for _, c in coded}
    if len(coded) < 3 or not any(len(c) > 1 and c[:-1] in codes for c in codes):
        return None
    first = coded[0][0]

    # Encabezado (si existe) para ubicar proveedor, documento, fecha y descripción
    name_i, doc_i, date_i, note_i = 1, None, None, None
    for r in rows[max(0, first - 6):first]:
        hdr = {k: next((j for j, c in enumerate(r) if rx.search(norm(c))), None)
               for k, rx in (("name", _NAME_HDR), ("doc", _DOC_HDR), ("date", _DATE_HDR), ("note", _NOTE_HDR))}
        if hdr["name"] is not None or hdr["note"] is not None:
            name_i = hdr["name"] if hdr["name"] is not None else name_i
            doc_i, date_i, note_i = hdr["doc"], hdr["date"], hdr["note"]
            break

    def cell(r: list[str], idx: int | None) -> str:
        return r[idx] if idx is not None and idx < len(r) else ""

    def last_amount(r: list[str]) -> tuple[Decimal | None, int | None]:
        for j in range(len(r) - 1, 0, -1):
            if r[j]:
                a = parse_amount(r[j])
                return (a, j) if a is not None else (None, None)
        return None, None

    title_cells = [c for r in rows[:first] for c in r if c]
    title = " · ".join(title_cells[:2]) if title_cells else None
    period = detect_period(" ".join(title_cells[:14])) or detect_period(filename)
    st = Statement(filename=filename, period=period, title=title)

    nodes: dict[tuple[int, ...], dict] = {}
    for i, c in coded:
        r = rows[i]
        amount, _ = last_amount(r)
        name = cell(r, name_i) or next((x for x in r[1:] if x and parse_amount(x) is None), "")
        nodes[c] = {"row": i + 1, "name": " ".join(name.split()), "amount": amount, "doc": cell(r, doc_i) or None,
                    "date": cell(r, date_i) or None, "note": cell(r, note_i) or None}
    if len(nodes) > MAX_ROWS:
        raise ExpenseFormatError(f"El archivo supera el máximo de {MAX_ROWS} partidas")

    def has_children(c: tuple[int, ...]) -> bool:
        return any(len(o) > len(c) and o[: len(c)] == c for o in nodes)

    for c, n in nodes.items():
        top = nodes.get(c[:1], {}).get("name") or str(c[0])
        if has_children(c):
            kids = [o for k, o in nodes.items() if len(k) == len(c) + 1 and k[: len(c)] == c]
            amounts = [k["amount"] for k in kids if k["amount"] is not None]
            if n["amount"] is not None and amounts and len(amounts) == len(kids):
                st.totals.append(DeclaredTotal("group", n["name"], n["amount"], top, n["row"], expected=sum(amounts, Decimal(0))))
        elif n["amount"] is not None and n["amount"] != 0 and n["name"]:
            group = nodes.get(c[:2], {}).get("name") if len(c) >= 3 else None
            st.items.append(Item(top, n["name"], n["amount"], n["row"], group, n["doc"], n["date"], n["note"]))

    # Filas sin código después del primer ítem: totales declarados y partidas sueltas (p. ej. fondo de reserva)
    for i in range(first + 1, len(rows)):
        r = rows[i]
        if not any(r) or _code(r[0]) is not None:
            continue
        amount, aj = last_amount(r)
        label = next((x for j, x in enumerate(r) if x and j != aj and parse_amount(x) is None), "")
        if amount is None or not label:
            continue
        if _TOTAL_LABEL.match(norm(label)):
            st.totals.append(DeclaredTotal("subtotal" if norm(label).startswith("sub") else "total", " ".join(label.split()), amount, None, i + 1))
        else:
            st.items.append(Item(UNSECTIONED, " ".join(label.split()), amount, i + 1))
    return [st] if st.items else None
