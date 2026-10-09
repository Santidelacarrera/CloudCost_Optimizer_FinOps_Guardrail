"""Medición del ahorro OBSERVADO tras un cambio desplegado, controlando período y uso. Funciones puras (sin base de datos).

Comparar «el coste de antes» con «el coste de después» engaña con facilidad: un mes tiene más días que otro, una instancia puede haber
estado apagada una semana, la carga puede haber subido, el precio unitario puede haber cambiado por un compromiso nuevo. Este módulo
no pretende demostrar causalidad —no se puede con datos de facturación— sino reducir lo que engaña y declarar lo que queda:

  PERÍODO  · ventanas de la misma longitud y múltiplo de 7 días (misma mezcla de días de la semana);
           · se descartan el día del despliegue y el siguiente (cobro parcial) y los últimos 2 días (la facturación se retrasa);
           · se compara el promedio diario, nunca el total de períodos de longitud distinta.
  USO      · si el recurso estuvo activo distinto número de días, se compara el coste por día ACTIVO y se proyecta a los días activos
             de la línea base («mismas horas que antes»);
           · se comprueba la utilización (CPU) frente a la esperada tras el cambio y se avisa si la carga se movió;
           · se compara con el resto de la cuenta en el mismo servicio (sin este recurso): si todo el servicio subió o bajó, el efecto
             no es atribuible solo al cambio.
  DATOS    · «facturación» (Cost Explorer, importación) vs «modelo» (tabla de precios): una cifra observada con datos de modelo es la
             estimación repetida, no una observación, y se marca como tal.

El resultado separa el ahorro BRUTO (diferencia directa), el AJUSTADO por uso (el que se compara con lo aprobado), el motivo por el que
puede no ser atribuible (`confounders`) y un grado de confianza.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import fmean
from typing import Any

from .pricing import DAYS_PER_MONTH, instance_spec

PRE_DAYS = 14                    # dos semanas completas antes del cambio
BUFFER_DAYS = 2                  # día del despliegue + el siguiente: cobro parcial / propagación
DATA_LAG_DAYS = 2                # la facturación de AWS llega con hasta 24-48 h de retraso
MIN_POST_DAYS = 7
MAX_POST_DAYS = 28
TOLERANCE = 0.15                 # desviación aceptable entre el coste posterior y el esperado por el cambio
ACTIVE_RATIO_SHIFT = 0.10        # el recurso estuvo activo >10 puntos de días distintos antes y después
USAGE_DRIFT = 0.50               # la CPU se desvió >50 % de la esperada tras el cambio
SERVICE_SHIFT_MEDIUM = 0.15
SERVICE_SHIFT_HIGH = 0.30
MIN_COVERAGE = 0.80              # fracción mínima de días con dato para fiarse de una ventana
RESIDUAL_FULL = 0.05             # tras eliminar, ≤5 % del coste anterior se considera eliminado del todo
BILLING_SOURCES = frozenset({"cost_explorer", "import"})

DELETE_ACTIONS = frozenset({"REMOVE_RESOURCE", "DELETE_VOLUME", "DELETE_SNAPSHOT", "DELETE_DB_INSTANCE"})
RESIZE_ACTIONS = frozenset({"RESIZE_INSTANCE"})


@dataclass(frozen=True)
class Windows:
    pre_start: date
    pre_end: date                # exclusivo
    post_start: date
    post_end: date               # exclusivo

    @property
    def pre_days(self) -> int:
        return (self.pre_end - self.pre_start).days

    @property
    def post_days(self) -> int:
        return (self.post_end - self.post_start).days

    def to_dict(self) -> dict[str, Any]:
        return {"pre": [self.pre_start.isoformat(), self.pre_end.isoformat()], "post": [self.post_start.isoformat(), self.post_end.isoformat()],
                "pre_days": self.pre_days, "post_days": self.post_days}


class InsufficientData(Exception):
    def __init__(self, message: str, available_days: int = 0):
        super().__init__(message)
        self.available_days = available_days


def plan_windows(deployed_on: date, today: date) -> Windows:
    """Ventanas de comparación: 14 días previos y entre 7 y 28 días posteriores, en semanas completas, sin los días de transición."""
    post_start = deployed_on + timedelta(days=BUFFER_DAYS)
    last_complete = today - timedelta(days=DATA_LAG_DAYS)
    available = (last_complete - post_start).days + 1
    if available < MIN_POST_DAYS:
        raise InsufficientData(
            f"Se necesitan al menos {MIN_POST_DAYS} días completos de costes posteriores al despliegue (descontando {BUFFER_DAYS} de "
            f"transición y {DATA_LAG_DAYS} de retraso de facturación): hay {max(available, 0)}", max(available, 0))
    post_days = min(MAX_POST_DAYS, (available // 7) * 7)
    return Windows(deployed_on - timedelta(days=PRE_DAYS), deployed_on, post_start, post_start + timedelta(days=post_days))


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


@dataclass
class Confounder:
    code: str
    severity: str                # low | medium | high
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass
class Measurement:
    attribution: str
    confidence_grade: str
    data_grade: str                              # billing | model
    baseline_monthly_cost: float
    observed_monthly_cost_raw: float
    observed_monthly_cost_adjusted: float
    raw_savings: float
    adjusted_savings: float                      # el que se compara con lo aprobado
    realization_pct: float | None
    confounders: list[Confounder] = field(default_factory=list)
    controls: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)


BASE_LIMITATIONS = (
    "La facturación no demuestra causalidad: una caída de coste tras un cambio es compatible con el cambio, no la prueba de que lo causó.",
    "Los costes de Cost Explorer pueden revisarse hasta unos días después; las cifras de los últimos días de la ventana pueden variar.",
    "Reserved Instances, Savings Plans, créditos y cambios de precio alteran el coste efectivo sin que el recurso cambie.",
)


def _mean(vals: list[float]) -> float:
    return fmean(vals) if vals else 0.0


def _series(daily: dict[date, float], days: list[date]) -> list[float | None]:
    return [daily.get(d) for d in days]


def _billing_share(sources: dict[date, str], days: list[date]) -> float:
    seen = [sources[d] for d in days if d in sources]
    return (sum(1 for s in seen if s in BILLING_SOURCES) / len(seen)) if seen else 0.0


def _service_trend(pre: list[float] | None, post: list[float] | None) -> float | None:
    if not pre or not post:
        return None
    base = _mean(pre)
    return (_mean(post) - base) / base if base > 0 else None


def measure(*, action: str, params: dict[str, Any], windows: Windows, approved_savings: float, baseline_fallback_monthly: float,
            resource_daily: dict[date, float], sources: dict[date, str], billing_days: set[date], resource_exists_now: bool | None,
            usage_pre: dict[str, Any] | None = None, usage_post: dict[str, Any] | None = None,
            peers_pre: list[float] | None = None, peers_post: list[float] | None = None,
            baseline_monthly_stored: float | None = None, baseline_grade_stored: str | None = None) -> Measurement:
    """Compara los costes de las ventanas previa y posterior. Ver el docstring del módulo para lo que controla."""
    pre_days, post_days = _days(windows.pre_start, windows.pre_end), _days(windows.post_start, windows.post_end)
    limitations = list(BASE_LIMITATIONS)
    confounders: list[Confounder] = []
    is_delete = action in DELETE_ACTIONS

    pre_obs = [v for v in _series(resource_daily, pre_days) if v is not None]
    # Tras eliminar el recurso Cost Explorer ya no lo lista: un día sin fila cuenta como 0 SOLO si ese día hay facturación de la cuenta.
    post_cells = _series(resource_daily, post_days)
    if is_delete:
        post_cells = [0.0 if v is None and d in billing_days else v for v, d in zip(post_cells, post_days, strict=True)]
    post_obs = [v for v in post_cells if v is not None]

    pre_cov, post_cov = len(pre_obs) / len(pre_days), len(post_obs) / len(post_days)
    # Tras eliminar, el recurso ya no tiene filas propias: su calidad se juzga por la ventana previa y por la cobertura de facturación de la cuenta.
    billing = _billing_share(sources, pre_days) if is_delete else min(_billing_share(sources, pre_days), _billing_share(sources, post_days))
    data_grade = "billing" if billing >= 0.8 else "model"
    if data_grade == "model":
        confounders.append(Confounder("model_data", "high", "Los costes de la ventana provienen de la tabla de precios, no de la facturación: "
                                                             "la cifra repite la estimación, no la observa."))

    # ---- línea base
    if pre_cov >= MIN_COVERAGE:
        baseline = _mean(pre_obs) * DAYS_PER_MONTH
        baseline_source = "window"
    elif baseline_monthly_stored is not None:
        baseline, baseline_source = baseline_monthly_stored, "stored_baseline"
        confounders.append(Confounder("baseline_from_snapshot", "medium", "Faltan costes diarios previos al cambio; se usó la línea base "
                                                                          "guardada al aprobar."))
        if baseline_grade_stored == "model":
            data_grade = "model"
    else:
        baseline, baseline_source = baseline_fallback_monthly, "approved_estimate"
        data_grade = "model"
        confounders.append(Confounder("baseline_from_estimate", "high", "No hay costes previos al cambio: la línea base es la estimación aprobada."))
    if post_cov < MIN_COVERAGE:
        confounders.append(Confounder("incomplete_post_data", "high", f"Solo hay costes de {len(post_obs)} de {len(post_days)} días posteriores."))
    elif post_cov < 1.0 or pre_cov < 1.0:
        confounders.append(Confounder("incomplete_data", "low", f"Cobertura de datos: {pre_cov:.0%} antes, {post_cov:.0%} después."))

    controls: dict[str, Any] = {"windows": windows.to_dict(), "baseline_source": baseline_source,
                                "coverage": {"pre": round(pre_cov, 3), "post": round(post_cov, 3)},
                                "billing_share": round(billing, 3), "buffer_days": BUFFER_DAYS, "data_lag_days": DATA_LAG_DAYS}

    if not post_obs:
        return Measurement("inconclusive", "low", data_grade, round(baseline, 2), 0.0, 0.0, 0.0, 0.0, None, confounders, controls,
                           limitations + ["No hay costes posteriores al cambio para comparar."])

    post_mean_all = _mean(post_obs)
    raw_observed = post_mean_all * DAYS_PER_MONTH
    raw_savings = baseline - raw_observed
    attribution, adjusted_observed = "inconclusive", raw_observed

    if is_delete:
        residual = raw_observed / baseline if baseline > 0 else 0.0
        controls["residual_cost_ratio"] = round(residual, 4)
        controls["resource_still_in_inventory"] = resource_exists_now
        if resource_exists_now:
            attribution = "not_applied"
            confounders.append(Confounder("resource_still_exists", "high", "El recurso sigue en el inventario del último escaneo: el cambio "
                                                                            "no parece aplicado."))
        elif residual <= RESIDUAL_FULL:
            attribution = "confirmed"
        else:
            attribution = "partial"
            confounders.append(Confounder("residual_cost", "medium", f"Tras eliminarlo queda un coste equivalente al {residual:.0%} del anterior "
                                                                      "(snapshot final, recursos asociados u otros conceptos)."))
        if resource_exists_now is None:
            confounders.append(Confounder("inventory_unknown", "medium", "No se pudo comprobar si el recurso sigue existiendo."))
    elif action in RESIZE_ACTIONS:
        cur, tgt = instance_spec(params.get("current_instance_type")), instance_spec(params.get("target_instance_type"))
        pre_pos = [v for v in pre_obs if v > 0]
        post_pos = [v for v in post_obs if v > 0]
        if cur is None or tgt is None or not pre_pos or not post_pos:
            return Measurement("inconclusive", "low", data_grade, round(baseline, 2), round(raw_observed, 2), round(raw_observed, 2),
                               round(raw_savings, 2), round(raw_savings, 2), None, confounders, controls,
                               limitations + ["No se conocen los precios de ambos tipos o falta actividad facturada en alguna ventana."])
        pre_active = len(pre_pos) / len(pre_obs)
        post_active = len(post_pos) / len(post_obs)
        pre_rate, post_rate = _mean(pre_pos), _mean(post_pos)
        ratio = tgt.hourly_usd / cur.hourly_usd
        expected_rate = pre_rate * ratio
        deviation = (post_rate - expected_rate) / expected_rate if expected_rate > 0 else 0.0
        adjusted_observed = post_rate * pre_active * DAYS_PER_MONTH            # mismas horas activas que en la línea base
        controls.update({"active_days_ratio": {"pre": round(pre_active, 3), "post": round(post_active, 3)},
                         "daily_rate_active": {"pre": round(pre_rate, 4), "post": round(post_rate, 4)},
                         "price_ratio": round(ratio, 4), "expected_post_daily_rate": round(expected_rate, 4),
                         "deviation_from_expected": round(deviation, 4)})
        if abs(post_active - pre_active) > ACTIVE_RATIO_SHIFT:
            confounders.append(Confounder("active_days_changed", "medium",
                                          f"El recurso estuvo activo el {pre_active:.0%} de los días antes y el {post_active:.0%} después: "
                                          "se comparó el coste por día activo, proyectado a los días activos de la línea base."))
        if abs(deviation) <= TOLERANCE:
            attribution = "confirmed"
        elif deviation > TOLERANCE:
            attribution = "not_applied" if post_rate >= pre_rate * (1 - TOLERANCE) else "partial"
        else:
            attribution = "exceeds_model"
            confounders.append(Confounder("saving_exceeds_model", "medium",
                                          f"El coste bajó un {abs(deviation):.0%} más de lo que explica el cambio de tamaño: puede haber "
                                          "otros factores (descuentos, compromisos, menor consumo)."))
        if usage_pre and usage_post and usage_pre.get("cpu_avg") and usage_post.get("cpu_avg"):
            expected_cpu = usage_pre["cpu_avg"] * cur.vcpu / tgt.vcpu
            drift = (usage_post["cpu_avg"] - expected_cpu) / expected_cpu if expected_cpu > 0 else 0.0
            controls["cpu"] = {"pre": usage_pre["cpu_avg"], "post": usage_post["cpu_avg"], "expected_post": round(expected_cpu, 2),
                               "drift": round(drift, 3)}
            if abs(drift) > USAGE_DRIFT:
                confounders.append(Confounder("usage_changed", "high" if drift > 0 else "medium",
                                              f"La CPU observada ({usage_post['cpu_avg']:.1f} %) difiere un {abs(drift):.0%} de la esperada tras el "
                                              f"cambio ({expected_cpu:.1f} %): la carga de trabajo cambió" +
                                              (" y el tipo menor podría quedarse corto." if drift > 0 else ".")))
    else:
        return Measurement("inconclusive", "low", "model", round(baseline, 2), round(raw_observed, 2), round(raw_observed, 2),
                           round(raw_savings, 2), round(raw_savings, 2), None, confounders, controls,
                           limitations + [f"La acción {action} no tiene coste facturado por recurso: solo puede declararse el ahorro manualmente."])

    trend = _service_trend(peers_pre, peers_post)
    if trend is not None:
        controls["service_peers_trend"] = round(trend, 4)
        if abs(trend) > SERVICE_SHIFT_MEDIUM:
            sev = "high" if abs(trend) > SERVICE_SHIFT_HIGH else "medium"
            confounders.append(Confounder("service_wide_change", sev,
                                          f"El resto de la cuenta en el mismo servicio {'subió' if trend > 0 else 'bajó'} un {abs(trend):.0%} "
                                          "en el mismo período: el cambio de coste no es atribuible solo a esta acción."))
            controls["control_adjusted_savings"] = round(baseline * (1 + trend) - raw_observed, 2)
    else:
        controls["service_peers_trend"] = None

    adjusted_savings = baseline - adjusted_observed
    if attribution in ("confirmed", "exceeds_model") and any(c.severity == "high" for c in confounders if c.code != "model_data"):
        attribution = "confounded"
    if data_grade == "model" and attribution in ("confirmed", "exceeds_model", "partial"):
        attribution = "inconclusive"

    high = any(c.severity == "high" for c in confounders)
    if data_grade == "billing" and not high and len(post_days) >= 14 and pre_cov >= 0.85 and post_cov >= 0.95 \
            and not any(c.severity == "medium" for c in confounders):
        grade = "high"
    elif data_grade == "billing" and not high:
        grade = "medium"
    else:
        grade = "low"
    pct = round(adjusted_savings / approved_savings * 100, 1) if approved_savings > 0 else None
    return Measurement(attribution, grade, data_grade, round(baseline, 2), round(raw_observed, 2), round(adjusted_observed, 2),
                       round(raw_savings, 2), round(adjusted_savings, 2), pct, confounders, controls, limitations)
