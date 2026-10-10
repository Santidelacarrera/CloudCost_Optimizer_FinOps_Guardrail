"""Validación del colector de AWS contra una CUENTA REAL de laboratorio, con un informe reproducible.

Qué hace
    1. Con credenciales de SOLO LECTURA (rol con ExternalId o la cadena por defecto de boto3) ejecuta el mismo `AwsCollector` que usa el
       producto y anota cada operación de AWS que invoca (nombre, número, errores). La guardia de solo lectura del colector rechaza
       cualquier otra.
    2. Guarda una INSTANTÁNEA (`snapshot.json`): inventario, costes, incidencias clasificadas, cobertura y hallazgos calculados.
    3. Genera un informe en Markdown a partir de la instantánea, de forma DETERMINISTA: la misma instantánea produce el mismo informe,
       byte a byte (se guarda su SHA-256). Así cualquiera puede regenerarlo (`--from-snapshot`) y comprobar que coincide, sin acceso a la cuenta.

Qué NO hace: no escribe en AWS, no usa la base de datos, no aprueba ni propone ningún cambio. Ver docs/aws-lab-validation.md.

Anonimización (`anonymize=True`): los identificadores de recursos y nombres se sustituyen por seudónimos estables (HMAC con una sal que no
se guarda), la cuenta queda como `****1234`, y se descartan los valores de las etiquetas salvo el entorno. Los importes se conservan
(o se multiplican por `scale` para ocultar el gasto real manteniendo las proporciones).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from . import redaction
from .collectors import aws_guard
from .collectors.aws import AwsAccessError, AwsCollector
from .collectors.base import CollectionResult
from .domain import risk as risk_mod
from .domain.models import NormalizedResource
from .domain.rules import Finding, RuleConfig, evaluate_all
from .secrets import SecretResolver

SNAPSHOT_VERSION = 1
TOOL = "cloudcost-aws-lab"


@dataclass
class LabConfig:
    account_ref: str
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    role_arn: str | None = None
    external_id_env: str | None = None             # NOMBRE de la variable de entorno CC_SECRET_* con el ExternalId (nunca el valor)
    months: int = 6
    ce_request_budget: int = 40
    use_cost_explorer: bool = True
    include_rds: bool = False                       # exige rds:DescribeDBInstances en el rol
    cost_tag_key: str | None = None
    anonymize: bool = False
    salt: str | None = None
    scale: float = 1.0
    today: date | None = None                       # solo pruebas: fija «hoy» para que las consultas a Cost Explorer sean deterministas
    sleep: Any = None                               # solo pruebas: sustituye las pausas (límite de CloudTrail LookupEvents)


# --------------------------------------------------------------------------- recolección
class CallRecorder:
    """Anota las operaciones de AWS realmente enviadas (servicio, operación, errores) engancharse a los eventos de botocore."""

    def __init__(self):
        self.counts: Counter[tuple[str, str]] = Counter()
        self.errors: Counter[tuple[str, str]] = Counter()
        self._pending: dict[int, tuple[str, str]] = {}

    def install(self, session) -> None:
        events = getattr(session, "events", None)
        if events is None:
            return
        events.register("before-call.*.*", self._before, unique_id="lab-recorder-before")
        events.register("after-call.*.*", self._after, unique_id="lab-recorder-after")

    def _before(self, model, **_: Any) -> None:
        self.counts[(model.service_model.service_name, model.name)] += 1

    def _after(self, model, parsed, **_: Any) -> None:
        if isinstance(parsed, dict) and "Error" in parsed:
            self.errors[(model.service_model.service_name, model.name)] += 1

    def to_list(self) -> list[dict[str, Any]]:
        return [{"service": s, "operation": o, "count": n, "errors": self.errors.get((s, o), 0),
                 "iam_action": aws_guard.ALLOWED_OPERATIONS.get((s, o), "?")} for (s, o), n in sorted(self.counts.items())]


def _finding_dict(f: Finding, risk: str, account_ref: str) -> dict[str, Any]:
    est = f.evidence.get("estimate") or {}
    return {"rule_id": f.rule_id, "action": f.action, "resource_id": f.resource.resource_id, "title": f.title,
            "current_monthly_cost": f.current_monthly_cost, "projected_monthly_cost": f.projected_monthly_cost,
            "estimated_monthly_savings": f.estimated_monthly_savings, "confidence": f.confidence, "risk": risk, "destructive": f.destructive,
            "environment": f.resource.environment, "cost_source": f.resource.cost_source, "dedupe_key": f.dedupe_key(account_ref),
            "formula_id": est.get("formula_id"), "formula": est.get("expression"), "kind": est.get("kind"),
            "reference": est.get("reference"), "assumptions": est.get("assumptions"), "limitations": est.get("limitations"),
            "confidence_adjustments": f.evidence.get("confidence_adjustments", [])}


def _resource_dict(r: NormalizedResource) -> dict[str, Any]:
    d = r.to_dict()
    d["attributes"] = {k: v for k, v in d["attributes"].items() if k in ("cost_basis", "managed_by", "ami_ids", "cost_history")}
    return d


def collect_snapshot(cfg: LabConfig, *, session=None, now: datetime | None = None, rule_config: RuleConfig | None = None) -> dict[str, Any]:
    """Ejecuta el colector real y devuelve la instantánea (serializable a JSON)."""
    import boto3

    started = now or datetime.now(timezone.utc)
    recorder = CallRecorder()
    secrets = SecretResolver()
    account: dict[str, Any] = {"regions": cfg.regions, "role_arn": cfg.role_arn, "account_ref": cfg.account_ref}
    if cfg.external_id_env:
        account["external_id_ref"] = f"env:{cfg.external_id_env}"
    if session is None and not cfg.role_arn:
        session = boto3.Session()
    if session is not None:
        recorder.install(session)
    collector = AwsCollector(account, secrets, use_cost_explorer=cfg.use_cost_explorer, session=session, cost_tag_key=cfg.cost_tag_key,
                             cost_history_months=cfg.months, ce_request_budget=cfg.ce_request_budget, include_rds=cfg.include_rds,
                             today=(lambda: cfg.today) if cfg.today else date.today, **({"sleep": cfg.sleep} if cfg.sleep else {}))
    aborted: str | None = None
    result = CollectionResult()
    try:
        if session is None:                                   # con rol: la sesión la construye el colector tras AssumeRole; se engancha el registro al crearla
            collector._session = collector._build_session()
            recorder.install(collector._session)
        result = collector.collect()
    except AwsAccessError as exc:
        aborted = str(exc)
        result.issues = [exc.issue.to_dict()] if exc.issue else []
    findings: list[tuple[Finding, str]] = []
    for f in evaluate_all(result.resources, rule_config or RuleConfig()):
        findings.append((f, risk_mod.classify_risk(f)))
    acct = cfg.account_ref
    snap: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "meta": {"tool": TOOL, "generated_at": started.replace(microsecond=0).isoformat(), "account_last4": acct[-4:], "regions": sorted(cfg.regions),
                 "identity_verified": collector.identity_verified, "cost_explorer": cfg.use_cost_explorer, "months": cfg.months,
                 "ce_request_budget": cfg.ce_request_budget, "aborted": aborted, "anonymized": False, "scale": 1.0,
                 "python": platform.python_version(), "boto3": boto3.__version__},
        "api_calls": recorder.to_list(),
        "issues": result.issues, "warnings": [redaction.redact_text(w) for w in result.warnings], "partial": result.partial,
        "data_quality": result.data_quality,
        "resources": [_resource_dict(r) for r in sorted(result.resources, key=lambda r: (r.service, r.resource_id))],
        "resource_costs": [[c.resource_id, c.usage_date.isoformat(), round(c.amount, 4), c.source] for c in
                           sorted(result.costs, key=lambda c: (c.resource_id, c.usage_date)) if c.source != "estimate"],
        "account_costs": [{**asdict(c), "period_start": c.period_start.isoformat(), "period_end": c.period_end.isoformat()}
                          for c in sorted(result.account_costs, key=lambda c: (c.granularity, c.period_start, c.service_raw, c.region))],
        "findings": sorted((_finding_dict(f, risk, acct) for f, risk in findings), key=lambda d: (-d["estimated_monthly_savings"], d["resource_id"], d["rule_id"])),
    }
    return anonymize(snap, cfg.salt, cfg.scale) if cfg.anonymize else snap


# --------------------------------------------------------------------------- anonimización
def anonymize(snap: dict[str, Any], salt: str | None = None, scale: float = 1.0) -> dict[str, Any]:
    """Copia anonimizada: seudónimos estables para identificadores y nombres, sin valores de etiquetas (salvo entorno), importes escalados."""
    key = (salt or os.urandom(16).hex()).encode()

    def pseudo(prefix: str, value: str | None) -> str | None:
        if not value:
            return value
        return f"{prefix}-{hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()[:8]}"

    ids: dict[str, str] = {}

    def rid(value: str) -> str:
        if value not in ids:
            kind = value.split("-", 1)[0] if "-" in value else "res"
            ids[value] = pseudo(kind, value) or value
        return ids[value]

    out = json.loads(json.dumps(snap))                                    # copia profunda
    out["meta"].update(anonymized=True, scale=scale)
    for r in out["resources"]:
        r["resource_id"] = rid(r["resource_id"])
        r["name"] = pseudo("name", r.get("name"))
        r["tags"] = {k: v for k, v in (r.get("tags") or {}).items() if k.lower() in ("environment", "env", "stage")}
        r["iac_address"] = r["iac_file"] = None
        r["monthly_cost"] = round(r["monthly_cost"] * scale, 2)
        r["attributes"] = {k: v for k, v in (r.get("attributes") or {}).items() if k == "cost_basis"}
        basis = r["attributes"].get("cost_basis")
        if basis and "last_14d_total" in basis:
            basis["last_14d_total"] = round(basis["last_14d_total"] * scale, 2)
    out["resource_costs"] = [[rid(a), b, round(c * scale, 4), d] for a, b, c, d in out["resource_costs"]]
    for c in out["account_costs"]:
        c["account_ref"] = "****" + str(c["account_ref"])[-4:]
        c["amount"] = round(c["amount"] * scale, 4)
    for f in out["findings"]:
        f["title"] = f"{f['rule_id']}: {rid(f['resource_id'])}"
        f["resource_id"] = rid(f["resource_id"])
        f["dedupe_key"] = pseudo("rec", f["dedupe_key"])
        for k in ("current_monthly_cost", "projected_monthly_cost", "estimated_monthly_savings"):
            f[k] = round(f[k] * scale, 2)
        ref = f.get("reference") or {}
        if "monthly_cost" in ref:
            ref["monthly_cost"] = round(ref["monthly_cost"] * scale, 2)
        f["reference"] = ref
    out["warnings"] = [redaction.redact_text(w) for w in out["warnings"]]
    out["data_quality"] = json.loads(json.dumps(out["data_quality"]))
    rq = out["data_quality"].get("cost_explorer", {}).get("resource_costs", {})
    if "cost_without_inventory_usd_14d" in rq:
        rq["cost_without_inventory_usd_14d"] = round(rq["cost_without_inventory_usd_14d"] * scale, 2)
    return out


def snapshot_digest(snap: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- informe (determinista)
def _usd(v: float) -> str:
    return f"{v:,.2f}"


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_report(snap: dict[str, Any]) -> str:
    """Markdown determinista: depende SOLO de la instantánea (nada de reloj, red ni orden de diccionarios)."""
    m, dq = snap["meta"], snap.get("data_quality") or {}
    ce = dq.get("cost_explorer") or {}
    res, findings, issues = snap["resources"], snap["findings"], snap["issues"]
    L: list[str] = []
    a = L.append
    a(f"# Validación del colector de AWS · cuenta ****{m['account_last4']}")
    a("")
    anon = ""
    if m["anonymized"]:
        anon = "**Datos anonimizados**" + (" (importes ×%g)" % m["scale"] if m["scale"] != 1.0 else "") + ". "
    a(f"> Generado el {m['generated_at']} con `{m['tool']}` (Python {m['python']}, boto3 {m['boto3']}). {anon}"
      f"Huella de la instantánea: `{snapshot_digest(snap)}`.")
    a("")
    a("**Cómo reproducir este informe:** `python scripts/aws_lab.py --from-snapshot snapshot.json` genera exactamente el mismo texto "
      "(su SHA-256 está en `report.sha256`). No hace falta acceso a la cuenta.")
    a("")
    a("## 1. Resultado de la validación")
    a("")
    verdict = "**INCOMPLETA**" if m["aborted"] else "**con incidencias**" if issues or snap["partial"] else "**sin incidencias**"
    a(f"- Identidad de la cuenta verificada con `sts:GetCallerIdentity`: {'sí' if m['identity_verified'] else 'no'}.")
    no_calls = not snap["api_calls"]
    ce_txt = "no consultado (la validación se interrumpió antes)" if no_calls else "solicitado" if m["cost_explorer"] else "desactivado por opción"
    inv_txt = "no obtenido (la validación se interrumpió antes de leer)" if no_calls else "parcial" if snap["partial"] else "completo"
    a(f"- Regiones: {', '.join(m['regions'])}. Cost Explorer: {ce_txt}.")
    a(f"- Resultado: {verdict}. Inventario {inv_txt}.")
    if m["aborted"]:
        a(f"- Motivo de la interrupción: {_md(m['aborted'])}")
    a("")
    a("## 2. Operaciones de AWS invocadas")
    a("")
    a("Todas son lecturas de una lista cerrada; una operación fuera de ella la rechaza el propio colector antes de enviarla.")
    a("")
    a("| Servicio | Operación | Acción IAM | Llamadas | Errores |")
    a("|---|---|---|---:|---:|")
    for c in snap["api_calls"]:
        a(f"| {c['service']} | {c['operation']} | `{c['iam_action'] or 'sin permiso IAM'}` | {c['count']} | {c['errors']} |")
    if not snap["api_calls"]:
        a("| — | — | — | 0 | 0 |")
    a("")
    a(f"Escrituras o modificaciones: **0** (operaciones fuera de la lista: {sum(1 for c in snap['api_calls'] if c['iam_action'] == '?')}).")
    a("")
    a("## 3. Inventario")
    a("")
    by_service = Counter((r["service"], r["state"] or "—") for r in res)
    a("| Servicio | Estado | Recursos |")
    a("|---|---|---:|")
    for (svc, state), n in sorted(by_service.items()):
        a(f"| {svc} | {state} | {n} |")
    if not res:
        a("| — | — | 0 |")
    a("")
    by_cost = Counter(r["cost_source"] for r in res)
    a("Origen del coste de cada recurso: " + (", ".join(f"{k} = {v}" for k, v in sorted(by_cost.items())) or "—") + ".")
    a("")
    a("## 4. Cobertura de Cost Explorer")
    a("")
    rc = ce.get("resource_costs") or {}
    if rc.get("available"):
        a(f"- Coste por recurso: {rc['matched']} recursos con coste real; {rc['inventory_without_cost']} del inventario sin coste; "
          f"{rc['cost_without_inventory']} con coste pero fuera del inventario (USD {_usd(rc.get('cost_without_inventory_usd_14d', 0))} en 14 días).")
        a(f"- Recursos con señales de calidad en la serie diaria: {rc['with_quality_flags']}. Ventana: {' → '.join(rc.get('window', []))}.")
    else:
        a("- Coste por recurso: **no disponible** (ver incidencias). Se usó la tabla de precios y se marcó como estimado.")
    ac = ce.get("account_costs") or {}
    if ac:
        a(f"- Coste de la cuenta: {ac['rows']} filas (diario {'sí' if ac['daily_available'] else 'no'}, mensual {'sí' if ac['monthly_available'] else 'no'}); "
          f"monedas: {', '.join(ac['currencies']) or '—'}; filas aún estimadas por AWS: {ac['estimated_rows']}; filas negativas (créditos): {ac['negative_rows']}.")
    if ce:
        a(f"- Solicitudes a Cost Explorer: {ce.get('requests', 0)} de un presupuesto de {ce.get('request_budget', '—')} "
          f"(≈ USD {ce.get('requests', 0) * 0.01:.2f}).")
    a("")
    _cost_tables(a, snap)
    a("## 6. Hallazgos (ahorro ESTIMADO, no observado)")
    a("")
    total = sum(f["estimated_monthly_savings"] for f in findings)
    verified = sum(f["estimated_monthly_savings"] for f in findings if (f.get("reference") or {}).get("verified"))
    a(f"{len(findings)} hallazgos; ahorro estimado **USD {_usd(total)}/mes**, de los cuales USD {_usd(verified)} se apoyan en coste real verificado.")
    a("")
    a("| Regla | Recurso | Entorno | Riesgo | Coste ref. | Ahorro/mes | Confianza | Base del coste |")
    a("|---|---|---|---|---:|---:|---:|---|")
    for f in findings:
        ref = f.get("reference") or {}
        basis = ("real" if ref.get("verified") else "estimado") + (f" ({', '.join(ref['quality_flags'])})" if ref.get("quality_flags") else "")
        a(f"| {f['rule_id']} | {_md(f['resource_id'])} | {f['environment']} | {f['risk']} | {_usd(f['current_monthly_cost'])} | "
          f"{_usd(f['estimated_monthly_savings'])} | {f['confidence']:.0%} | {basis} |")
    if not findings:
        a("| — | — | — | — | — | — | — | — |")
    a("")
    if findings:
        a("Fórmulas aplicadas: " + ", ".join(sorted({f"`{f['formula_id']}`" for f in findings if f.get('formula_id')})) + ". Detalle y supuestos: docs/savings-methodology.md.")
        a("")
    a("## 7. Incidencias clasificadas")
    a("")
    if issues:
        a("| API | Tipo | Código AWS | Reintentos | Qué hacer |")
        a("|---|---|---|---:|---|")
        for i in issues:
            a(f"| {_md(i['api'])}{' [' + i['region'] + ']' if i.get('region') else ''} | {i['kind']} | {i['code']} | {i.get('attempts', 1)} | {_md(i['hint'])} |")
    else:
        a("Ninguna.")
    a("")
    if snap["warnings"]:
        a("Avisos:")
        a("")
        for w in snap["warnings"]:
            a(f"- {_md(w)}")
        a("")
    a("## 8. Limitaciones de esta validación")
    a("")
    for line in (
        "Valida el colector contra UNA cuenta y las regiones indicadas; no demuestra que funcione igual en cuentas con Organizations, miles de recursos o servicios no soportados.",
        "Solo cubre EC2, EBS y snapshots; RDS, S3, Lambda y otros servicios no se inventarían (sí aparecen en el coste de la cuenta, agregados por servicio).",
        "Los costes de Cost Explorer pueden revisarse hasta unos días después; las filas marcadas como estimadas pueden cambiar.",
        "El ahorro de los hallazgos es una estimación con la fórmula indicada, no un ahorro observado.",
        "Cost Explorer y CloudTrail LookupEvents tienen límites de solicitudes; con muchas cuentas o volúmenes puede hacer falta repartir los escaneos.",
    ):
        a(f"- {line}")
    a("")
    return "\n".join(L)


def _cost_tables(a, snap: dict[str, Any]) -> None:
    rows = snap["account_costs"]
    a("## 5. Coste de la cuenta por servicio y región")
    a("")
    if not rows:
        a("Sin datos de coste de la cuenta (ver incidencias).")
        a("")
        return
    monthly = defaultdict(float)
    by_region = defaultdict(float)
    months: set[str] = set()
    est = False
    for r in rows:
        if r["granularity"] == "MONTHLY":
            monthly[(r["service"], r["currency"], r["period_start"][:7])] += r["amount"]
            months.add(r["period_start"][:7])
            by_region[(r["region"], r["currency"])] += r["amount"]
        est = est or r["estimated"]
    cols = sorted(months)
    if cols:
        a("**Mensual por servicio** (meses completos):")
        a("")
        a("| Servicio | Moneda | " + " | ".join(cols) + " |")
        a("|---|---|" + "---:|" * len(cols))
        for svc, cur in sorted({(s, c) for s, c, _ in monthly}):
            a(f"| {svc} | {cur} | " + " | ".join(_usd(monthly.get((svc, cur, mth), 0.0)) for mth in cols) + " |")
        a("")
        a("**Mensual por región** (suma de los meses anteriores):")
        a("")
        a("| Región | Moneda | Total |")
        a("|---|---|---:|")
        for (region, cur), v in sorted(by_region.items()):
            a(f"| {region} | {cur} | {_usd(v)} |")
        a("")
    daily = [r for r in rows if r["granularity"] == "DAILY"]
    if daily:
        days = sorted({r["period_start"] for r in daily})
        tot: dict[tuple[str, str], float] = defaultdict(float)
        for r in daily:
            tot[(r["service"], r["currency"])] += r["amount"]
        a(f"**Diario, {days[0]} → {days[-1]}** ({len(days)} días con datos), total por servicio:")
        a("")
        a("| Servicio | Moneda | Total |")
        a("|---|---|---:|")
        for (svc, cur), v in sorted(tot.items(), key=lambda kv: (-kv[1], kv[0])):
            a(f"| {svc} | {cur} | {_usd(v)} |")
        a("")
    if est:
        a("_Algunas filas están marcadas por AWS como estimadas (período aún abierto)._")
        a("")
    a("")


def write_outputs(snap: dict[str, Any], out_dir: str, render=None) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    report = (render or render_report)(snap)
    paths = {"snapshot": os.path.join(out_dir, "snapshot.json"), "report": os.path.join(out_dir, "report.md"),
             "digest": os.path.join(out_dir, "report.sha256")}
    with open(paths["snapshot"], "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    with open(paths["report"], "w", encoding="utf-8") as fh:
        fh.write(report)
    with open(paths["digest"], "w", encoding="utf-8") as fh:
        fh.write(hashlib.sha256(report.encode("utf-8")).hexdigest() + "  report.md\n")
    return paths


def load_snapshot(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    if snap.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"Versión de instantánea no soportada: {snap.get('version')}")
    return snap
