"""Informe de demostración: ahorro ESTIMADO frente a APROBADO y OBSERVADO, con datos sintéticos de laboratorio.

Los escenarios pasan por las MISMAS funciones del producto —reglas (`domain.rules`, para el ahorro estimado) y medición
(`domain.measurement`, para el observado)— pero con series de coste diarias inventadas y deterministas. No son clientes, ni facturas, ni
ahorros reales: sirven para enseñar qué distingue el informe y qué dice de sus propios límites. Para el mismo código, el texto es idéntico
byte a byte; una prueba comprueba que el archivo publicado coincide con lo que genera este módulo.

    python -m cloudcost.demo_report > docs/demo/informe-ahorro-demo.md
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date

from .domain import measurement as ms
from .domain.models import NormalizedResource
from .domain.pricing import DAYS_PER_MONTH
from .domain.rules import RuleConfig, evaluate_resource

DEPLOY = date(2026, 9, 1)
TODAY = date(2026, 9, 30)
D = DAYS_PER_MONTH
USD = "USD"

ATTRIBUTION_LABEL = {
    "confirmed": "Coherente con el cambio", "partial": "Menor de lo esperado", "exceeds_model": "Mayor de lo que explica el cambio",
    "not_applied": "El cambio no parece aplicado", "confounded": "No atribuible solo al cambio", "inconclusive": "No concluyente",
    "unverified": "Declarado, no medido",
}
GRADE_LABEL = {"high": "alta", "medium": "media", "low": "baja"}


@dataclass
class Scenario:
    key: str
    title: str
    story: str
    resource: NormalizedResource
    action: str = ""
    params: dict = field(default_factory=dict)
    pre: float | list[float] = 10.0
    post: float | list[float] = 5.0
    source: str = "cost_explorer"
    peers_pre: list[float] | None = None
    peers_post: list[float] | None = None
    usage_pre: dict | None = None
    usage_post: dict | None = None
    exists_now: bool = True
    billing_after_delete: bool = False
    manual: float | None = None
    status: str = "deployed"                 # pending (sin decidir) | approved (aprobada, sin desplegar) | deployed


def _wiggle(base: float, n: int, amplitude: float = 0.03) -> list[float]:
    """Variación diaria determinista (±3 %): el coste real nunca es plano."""
    pattern = [0, 1, -1, 2, -2, 1, -1, 0, -2, 2, 1, -1, 0, 2]
    return [round(base * (1 + amplitude * pattern[i % len(pattern)] / 2), 4) for i in range(n)]


def _ec2(rid: str, itype: str, cost: float, env: str, cpu: float, mem: float) -> NormalizedResource:
    return NormalizedResource("aws", "compute", "ec2", rid, "us-east-1", name=rid, state="running", instance_type=itype, cpu_avg=cpu, cpu_max=cpu * 2.4,
                              memory_avg=mem, observation_days=30, environment=env, monthly_cost=cost, cost_source="cost_explorer", age_days=200,
                              attributes={"cost_basis": {"source": "cost_explorer", "window_days": 14, "window_start": "2026-08-18", "window_end": "2026-09-01",
                                                         "days_with_data": 14, "quality_flags": [], "as_of": "2026-09-01"}})


def _scenarios() -> list[Scenario]:
    resize = {"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"}
    monthly = 10.0 * D
    out = [
        Scenario("A", "Reducir api-staging: m5.2xlarge → m5.xlarge",
                 "Cambio aplicado sin incidencias; el coste baja lo que predice la proporción de precios.",
                 _ec2("i-api-staging", "m5.2xlarge", monthly, "staging", 12.4, 19.8), "RESIZE_INSTANCE", resize,
                 usage_pre={"cpu_avg": 12.4}, usage_post={"cpu_avg": 24.1}),
        Scenario("B", "Reducir batch-staging: m5.2xlarge → m5.xlarge",
                 "Igual que A, pero la instancia estuvo parada 5 días tras el cambio: la diferencia bruta se infla y el ajuste por uso la corrige.",
                 _ec2("i-batch-staging", "m5.2xlarge", monthly, "staging", 11.0, 18.0), "RESIZE_INSTANCE", resize, usage_pre={"cpu_avg": 11.0},
                 usage_post={"cpu_avg": 21.5}),
        Scenario("C", "Reducir reports-dev: m5.2xlarge → m5.xlarge",
                 "El Pull Request se fusionó pero el despliegue no llegó a producción: el coste no cambió.",
                 _ec2("i-reports-dev", "m5.2xlarge", monthly, "development", 9.5, 17.0), "RESIZE_INSTANCE", resize, usage_pre={"cpu_avg": 9.5},
                 usage_post={"cpu_avg": 9.6}, post=10.0),
        Scenario("D", "Reducir ml-staging: m5.2xlarge → m5.xlarge",
                 "El cambio se aplicó, pero ese mes el resto de EC2 en la cuenta subió un 45 % (cargas nuevas): no se puede atribuir el resultado solo al cambio.",
                 _ec2("i-ml-staging", "m5.2xlarge", monthly, "staging", 10.2, 16.0), "RESIZE_INSTANCE", resize, usage_pre={"cpu_avg": 10.2},
                 usage_post={"cpu_avg": 20.0}),
        Scenario("E", "Eliminar volumen huérfano vol-tmp-rollout",
                 "Volumen de un despliegue fallido: tras eliminarlo deja de facturarse y la cuenta sigue con facturación.",
                 NormalizedResource("aws", "storage", "ebs", "vol-tmp-rollout", "us-east-1", name="vol-tmp-rollout", state="available", attached=False,
                                    volume_type="gp3", size_gb=500, unattached_days=40, environment="staging", monthly_cost=1.5 * D,
                                    cost_source="cost_explorer", age_days=90, attributes={"cost_basis": {"source": "cost_explorer", "quality_flags": [],
                                                                                                           "as_of": "2026-09-01"}}),
                 "DELETE_VOLUME", {}, pre=1.5, post=0.0, exists_now=False, billing_after_delete=True),
        Scenario("F", "Eliminar snapshot antiguo snap-2025-q1",
                 "La estimación del snapshot es una COTA SUPERIOR (tamaño del volumen × precio); la facturación real era menor: la realización lo muestra.",
                 NormalizedResource("aws", "storage", "ebs_snapshot", "snap-2025-q1", "us-east-1", name="snap-2025-q1", state="completed", size_gb=400,
                                    age_days=520, environment="staging", monthly_cost=20.0, cost_source="estimate_upper_bound"),
                 "DELETE_SNAPSHOT", {}, pre=11.0 / D, post=0.0, exists_now=False, billing_after_delete=True),
        Scenario("G", "Eliminar base de datos sin uso orders-legacy-dev",
                 "Aprobada por dos personas y a la espera de despliegue: todavía NO hay nada que observar.",
                 NormalizedResource("aws", "database", "rds", "orders-legacy-dev", "us-east-1", name="orders-legacy-dev", state="available",
                                    instance_type="db.m5.large", size_gb=200, cpu_avg=1.0, environment="development", observation_days=30,
                                    monthly_cost=0.0, cost_source="estimate", attributes={"connections_avg": 0.0, "connections_max": 0}),
                 "DELETE_DB_INSTANCE", {}, status="approved"),
        Scenario("H", "Reducir cache-prod: m5.2xlarge → m5.xlarge",
                 "Nadie midió: una persona indicó al marcar el cambio como verificado que el coste posterior era de 190 USD/mes. Se registra como DECLARADO, no como observado.",
                 _ec2("i-cache-prod", "m5.2xlarge", monthly, "staging", 13.0, 21.0), "RESIZE_INSTANCE", resize, manual=190.0),
        Scenario("I", "Reducir dev-sandbox: m5.2xlarge → m5.xlarge",
                 "Los costes de la ventana vienen de la tabla de precios (la cuenta no tenía Cost Explorer por recurso): repetir la estimación no es observar.",
                 _ec2("i-dev-sandbox", "m5.2xlarge", monthly, "development", 12.4, 19.8), "RESIZE_INSTANCE", resize, source="estimate"),
        Scenario("J", "Eliminar instancia ociosa old-batch",
                 "Detectada hace unos días y pendiente de aprobación: es solo una proyección, nadie la ha decidido todavía.",
                 _ec2("i-old-batch", "m5.2xlarge", monthly, "staging", 1.0, 6.0), "REMOVE_RESOURCE", {}, status="pending"),
    ]
    return out


def _series(w: ms.Windows, sc: Scenario) -> tuple[dict[date, float], dict[date, str]]:
    pre_days, post_days = ms._days(w.pre_start, w.pre_end), ms._days(w.post_start, w.post_end)
    pre = sc.pre if isinstance(sc.pre, list) else _wiggle(sc.pre, len(pre_days))
    post = sc.post if isinstance(sc.post, list) else _wiggle(sc.post, len(post_days))
    if sc.key == "B":                                              # parada de 5 días a mitad de la ventana posterior
        post = [0.0 if 6 <= i < 11 else v for i, v in enumerate(post)]
    daily = {d: v for d, v in zip(pre_days, pre, strict=True)}
    daily.update({d: v for d, v in zip(post_days, post, strict=True)})
    if sc.exists_now is False:                                     # recurso eliminado: Cost Explorer ya no lo lista
        for d in post_days:
            daily.pop(d, None)
    return daily, {d: sc.source for d in daily}


def _peers(sc: Scenario, w: ms.Windows) -> tuple[list[float] | None, list[float] | None]:
    if sc.key == "D":
        return [90.0] * w.pre_days, [90.0 * 1.45] * w.post_days
    if sc.key in ("A", "B", "C"):
        return [100.0 + (i % 3) for i in range(w.pre_days)], [101.0 + (i % 3) for i in range(w.post_days)]
    return None, None


def _estimate(sc: Scenario) -> float:
    findings = evaluate_resource(sc.resource, RuleConfig())
    return findings[0].estimated_monthly_savings if findings else 0.0


def _usd(v: float) -> str:
    return f"{v:,.2f}"


def build() -> str:
    w = ms.plan_windows(DEPLOY, TODAY)
    rows, details = [], []
    tot = {"estimated_open": 0.0, "approved_open": 0.0, "attributed": 0.0, "attributed_expected": 0.0, "confounded": 0.0, "declared": 0.0}
    for sc in _scenarios():
        est = _estimate(sc)
        approved = est                                   # congelado al aprobar: lo que vio la persona que aprobó
        if sc.status != "deployed":
            tot["approved_open" if sc.status == "approved" else "estimated_open"] += approved
            rows.append((sc, est, approved if sc.status == "approved" else None, None))
            continue
        if sc.manual is not None:
            baseline = sc.resource.monthly_cost
            observed_savings = baseline - sc.manual
            tot["declared"] += observed_savings
            rows.append((sc, est, approved, ("manual", baseline, observed_savings, observed_savings, round(observed_savings / approved * 100, 1))))
            continue
        daily, sources = _series(w, sc)
        pre_p, post_p = _peers(sc, w)
        billing_days = set(ms._days(w.post_start, w.post_end)) if sc.billing_after_delete else set()
        m = ms.measure(action=sc.action, params=sc.params, windows=w, approved_savings=approved, baseline_fallback_monthly=sc.resource.monthly_cost,
                       resource_daily=daily, sources=sources, billing_days=billing_days, resource_exists_now=sc.exists_now,
                       usage_pre=sc.usage_pre, usage_post=sc.usage_post, peers_pre=pre_p, peers_post=post_p)
        if m.data_grade == "billing" and m.attribution in ("confirmed", "partial", "exceeds_model", "not_applied"):
            tot["attributed"] += m.adjusted_savings
            tot["attributed_expected"] += approved
        elif m.data_grade == "billing" and m.attribution == "confounded":
            tot["confounded"] += m.adjusted_savings
        else:
            tot["declared"] += m.adjusted_savings
        rows.append((sc, est, approved, m))
        details.append((sc, est, approved, m))

    L: list[str] = []
    a = L.append
    a("# Informe de demostración: ahorro estimado, aprobado y observado")
    a("")
    a("> **DATOS SINTÉTICOS DE LABORATORIO.** Los diez casos son inventados para enseñar cómo se separan y se explican las cifras. No son clientes, "
      "ni facturas, ni ahorros reales. Pasan por las mismas funciones del producto (reglas para el estimado, `domain/measurement.py` para el observado).")
    a("")
    a("## Las tres cifras (y por qué nunca se suman)")
    a("")
    a("| Cifra | Qué es | Qué NO es |")
    a("|---|---|---|")
    a("| **Estimado** | Lo que una regla calcula que se ahorraría si el cambio se aplicara y todo lo demás siguiera igual. Lleva fórmula, coste de referencia y supuestos. | Un ahorro conseguido. |")
    a("| **Aprobado** | El estimado **congelado** en el momento en que las personas autorizadas aprueban; queda ligado a la huella de la evidencia que vieron. | Una promesa: sigue siendo una proyección hasta que se despliega y se mide. |")
    a("| **Observado** | Lo que se mide DESPUÉS del despliegue con la facturación real: 14 días previos frente a 1-4 semanas posteriores, sin los días de transición, corregido por días activos y comparado con el resto de la cuenta. | Una prueba de causalidad: es compatible con el cambio, no demuestra que lo causara. |")
    a("")
    a(f"Ventanas de comparación (iguales en todos los casos): **antes** {w.pre_start} → {w.pre_end} (exclusivo, {w.pre_days} días) · "
      f"**después** {w.post_start} → {w.post_end} (exclusivo, {w.post_days} días, semanas completas). Importes en USD por mes (30,4375 días).")
    a("")
    a("## Resumen por caso")
    a("")
    a("| Caso | Estimado | Aprobado | Observado (ajustado) | Diferencia bruta | Realización | Lectura | Confianza |")
    a("|---|---:|---:|---:|---:|---:|---|---|")
    for sc, est, appr, m in rows:
        if m is None:
            a(f"| **{sc.key}** {sc.title} | {_usd(est)} | {_usd(appr) if appr is not None else '—'} | — | — | — | "
              f"{'Aprobado, sin desplegar: nada que observar' if appr is not None else 'Pendiente de aprobación: solo una proyección'} | — |")
        elif isinstance(m, tuple):
            _, base, sav, raw, pct = m
            a(f"| **{sc.key}** {sc.title} | {_usd(est)} | {_usd(appr)} | {_usd(sav)}¹ | {_usd(raw)} | {pct:g} % | {ATTRIBUTION_LABEL['unverified']} | baja |")
        else:
            pct = f"{m.realization_pct:g} %" if m.realization_pct is not None else "—"
            a(f"| **{sc.key}** {sc.title} | {_usd(est)} | {_usd(appr)} | {_usd(m.adjusted_savings)} | {_usd(m.raw_savings)} | {pct} | "
              f"{ATTRIBUTION_LABEL[m.attribution]} | {GRADE_LABEL[m.confidence_grade]} |")
    a("")
    a("¹ Cifra escrita a mano por una persona; la plataforma no la midió.")
    a("")
    a("## Totales: cada tipo de cifra por separado")
    a("")
    a("| Concepto | USD/mes | Casos |")
    a("|---|---:|---|")
    a(f"| Estimado, sin decidir | {_usd(tot['estimated_open'])} | J |")
    a(f"| Aprobado, sin verificar | {_usd(tot['approved_open'])} | G |")
    keys = lambda pred: ", ".join(sc.key for sc, _, _, m in rows if m is not None and not isinstance(m, tuple) and pred(m)) or "—"  # noqa: E731
    a(f"| **Observado atribuible al cambio** | **{_usd(tot['attributed'])}** | {keys(lambda m: m.data_grade == 'billing' and m.attribution in ('confirmed', 'partial', 'exceeds_model', 'not_applied'))} |")
    a(f"| Observado no atribuible solo al cambio | {_usd(tot['confounded'])} | {keys(lambda m: m.data_grade == 'billing' and m.attribution == 'confounded')} |")
    a(f"| Observado declarado o sin facturación | {_usd(tot['declared'])} | H, {keys(lambda m: not (m.data_grade == 'billing'))} |")
    real = tot["attributed"] / tot["attributed_expected"] * 100 if tot["attributed_expected"] else 0.0
    a("")
    a(f"**Realización del observado atribuible: {real:.1f} %** (observado atribuible ÷ aprobado de esos mismos casos). Mezclar las columnas daría un número "
      "más bonito y más falso: por eso el producto no lo calcula.")
    a("")
    a("## Detalle de la medición")
    for sc, _est, appr, m in details:
        a("")
        a(f"### {sc.key} · {sc.title}")
        a("")
        a(sc.story)
        a("")
        a(f"- Línea base: USD {_usd(m.baseline_monthly_cost)}/mes · observado bruto: USD {_usd(m.observed_monthly_cost_raw)}/mes · "
          f"observado ajustado por uso: USD {_usd(m.observed_monthly_cost_adjusted)}/mes.")
        a(f"- Ahorro: bruto USD {_usd(m.raw_savings)} · ajustado USD {_usd(m.adjusted_savings)} · aprobado USD {_usd(appr)}.")
        a(f"- Datos: **{'facturación' if m.data_grade == 'billing' else 'tabla de precios (no es una observación)'}** · lectura: "
          f"{ATTRIBUTION_LABEL[m.attribution]} · confianza {GRADE_LABEL[m.confidence_grade]}.")
        c = m.controls
        if "active_days_ratio" in c:
            a(f"- Días activos: antes {c['active_days_ratio']['pre']:.0%}, después {c['active_days_ratio']['post']:.0%} · "
              f"desviación respecto a lo que explica el cambio de tamaño: {c['deviation_from_expected']:+.1%}.")
        if c.get("service_peers_trend") is not None:
            a(f"- Resto de la cuenta en el mismo servicio: {c['service_peers_trend']:+.1%}"
              + (f" · ahorro corregido por esa tendencia: USD {_usd(c['control_adjusted_savings'])} (supone que el recurso habría evolucionado como el resto)"
                 if "control_adjusted_savings" in c else "") + ".")
        if "residual_cost_ratio" in c:
            a(f"- Coste residual tras eliminar: {c['residual_cost_ratio']:.1%} del anterior.")
        for f in m.confounders:
            a(f"- ⚠ **{f.code}** ({f.severity}): {f.detail}")
    a("")
    a("## Qué demuestra este informe y qué no")
    a("")
    for line in (
        "Demuestra que las cifras se separan, que cada una dice de dónde sale y que el sistema distingue «el cambio funcionó» de «no se aplicó», «otra cosa se movió» y «nadie lo midió».",
        "No demuestra ahorros reales: los datos son inventados. El primer informe con una cuenta real debe generarse con `python -m cloudcost.cli aws-lab` y, pasado el despliegue, con la verificación de ahorro del producto.",
        "Ningún método basado en facturación puede probar causalidad. Reserved Instances, Savings Plans, créditos, cambios de precio y de carga pueden mover el coste por su cuenta; los controles de este informe reducen ese riesgo, no lo eliminan.",
        "El ajuste por uso supone que el precio por hora activa no cambió por otros motivos; si cambió, aparece como desviación o como «mayor de lo que explica el cambio».",
        "Con menos de 7 días completos posteriores al despliegue (descontando transición y retraso de facturación) el producto se niega a calcular en lugar de dar una cifra frágil.",
    ):
        a(f"- {line}")
    a("")
    a("## Cómo reproducirlo")
    a("")
    a("```bash")
    a("python -m cloudcost.demo_report > docs/demo/informe-ahorro-demo.md   # mismo código → mismo texto, byte a byte")
    a("pytest tests/unit/test_demo_report.py                                # comprueba que este archivo está al día")
    a("```")
    a("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.stdout.write(build())
