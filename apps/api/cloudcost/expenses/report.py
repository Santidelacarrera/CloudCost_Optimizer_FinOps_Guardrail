"""Informe Markdown (determinista) del análisis de gastos: el mismo contenido que la pantalla «Analizar gastos», para usarlo desde la terminal."""
from __future__ import annotations

from decimal import Decimal

from .analyzer import clp
from .cloud import _fmt


def _md(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _pct(share: str) -> str:
    return f"{(Decimal(share) * 100).quantize(Decimal('0.1'))} %".replace(".", ",")


def render_markdown(result: dict, title: str = "Análisis de gastos") -> str:
    L: list[str] = []
    a = L.append
    a(f"# {title}")
    a("")
    a(f"- Archivos con gastos: {len(result['statements'])} · estados de obra: {len(result['projects'])} · exportaciones de nube: {len(result['cloud'])}")
    a(f"- Hallazgos: **{result['total_findings']}** · subtotales y totales que cuadran: {result['checks']['passed']} de "
      f"{result['checks']['passed'] + result['checks']['failed']}")
    a("")
    a("> Los hallazgos son pistas para revisar, no conclusiones: que un gasto sobre o no depende de contexto que el archivo no trae.")
    a("")
    if result["findings"]:
        a("## Hallazgos (gastos y presupuestos)")
        a("")
        a("| Prioridad | Hallazgo | Período | Monto |")
        a("|---|---|---|---:|")
        label = {"alert": "Revisar primero", "review": "Verificar", "info": "Contexto"}
        for f in result["findings"]:
            a(f"| {label[f['severity']]} | **{_md(f['title'])}** — {_md(f['detail'])} | {f['statement'] or '—'} | {clp(Decimal(f['amount'])) if f['amount'] else '—'} |")
        a("")
    for st in result["statements"]:
        a(f"## {st['period'] or st['filename']} · total {clp(Decimal(st['total']))} · {st['item_count']} partidas")
        a("")
        a("| Sección | Total | % | Partidas |")
        a("|---|---:|---:|---:|")
        for s in st["sections"]:
            a(f"| {_md(s['name'])} | {clp(Decimal(s['total']))} | {_pct(s['share'])} | {s['item_count']} |")
        a("")
    if result["comparison"]:
        cmp = result["comparison"]
        a("## Comparación entre meses")
        a("")
        a("| Partida | " + " | ".join(cmp["periods"]) + " |")
        a("|---|" + "---:|" * len(cmp["periods"]))
        for r in cmp["rows"][:40]:
            a(f"| {_md(r['label'])} | " + " | ".join(clp(Decimal(x)) if x is not None else "—" for x in r["amounts"]) + " |")
        a("| **Total** | " + " | ".join(f"**{clp(Decimal(t))}**" for t in cmp["totals"]) + " |")
        a("")
    for c in result["cloud"]:
        cur = c["currency"]
        a(f"## Nube · {_md(c['filename'])} ({c['provider'].upper()}, {c['format']})")
        a("")
        a(f"- Moneda: **{cur}** · filas: {c['rows']} · granularidad: {c['granularity']} · {c['first_day']} → {c['last_day']}")
        a(f"- Gasto neto: **{cur} {_fmt(Decimal(c['total']))}**"
          + (f" · créditos/reembolsos: {cur} {_fmt(Decimal(c['credits']))}" if Decimal(c['credits']) else "")
          + (f" · impuestos: {cur} {_fmt(Decimal(c['tax']))}" if Decimal(c['tax']) else ""))
        if c["compared"]:
            a(f"- Meses comparados (solo completos): {c['compared'][0]} → {c['compared'][1]}")
        a("")
        a("| Mes | Gasto | Estado |")
        a("|---|---:|---|")
        for m in c["months"]:
            a(f"| {m['period']} | {_fmt(Decimal(m['total']))} | {'completo' if m['complete'] else 'incompleto'} |")
        a("")
        for key, heading in (("by_service", "Por servicio"), ("by_region", "Por región"), ("by_account", "Por cuenta/suscripción/proyecto"),
                             ("by_group", "Por grupo de recursos")):
            if c[key]:
                a(f"### {heading}")
                a("")
                a("| Nombre | Total | % |")
                a("|---|---:|---:|")
                for r in c[key]:
                    a(f"| {_md(r['name'])} | {_fmt(Decimal(r['total']))} | {_pct(r['share'])} |")
                a("")
        if c["movers"]:
            a("### Qué cambió entre los dos últimos meses completos")
            a("")
            a("| Servicio | Antes | Ahora | Variación | % |")
            a("|---|---:|---:|---:|---:|")
            for m in c["movers"]:
                a(f"| {_md(m['name'])} | {_fmt(Decimal(m['previous']))} | {_fmt(Decimal(m['current']))} | {_fmt(Decimal(m['delta']))} | "
                  f"{_pct(m['pct']) if m['pct'] is not None else 'nuevo'} |")
            a("")
        if c["anomalies"]:
            a("### Picos diarios")
            a("")
            a("| Servicio | Día | Costo | Mediana previa |")
            a("|---|---|---:|---:|")
            for x in c["anomalies"]:
                a(f"| {_md(x['name'])} | {x['day']} | {_fmt(Decimal(x['amount']))} | {_fmt(Decimal(x['baseline']))} |")
            a("")
        if c["findings"]:
            a("### Hallazgos")
            a("")
            label = {"alert": "Revisar primero", "review": "Verificar", "info": "Contexto"}
            for f in c["findings"]:
                a(f"- **{label[f['severity']]} · {_md(f['title'])}** — {_md(f['detail'])}")
            a("")
        for n in c["notes"]:
            a(f"- _Nota:_ {_md(n)}")
        a("")
        a("_Esto es análisis de FACTURA: dice dónde y cuándo cambió el gasto, no qué recurso concreto lo causa ni cuánto se puede ahorrar. "
          "Para eso hay que conectar la cuenta o importar el inventario de recursos._")
        a("")
    return "\n".join(L)
