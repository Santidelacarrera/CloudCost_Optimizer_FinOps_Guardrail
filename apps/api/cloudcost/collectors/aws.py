"""Conector AWS de SOLO LECTURA: inventario EC2/EBS/snapshots + métricas de CloudWatch + costos de Cost Explorer.

Permisos necesarios: ver infrastructure/terraform/aws-readonly-role. El conector nunca llama a APIs de escritura: la sesión de boto3
lleva una guardia (`aws_guard`) que rechaza cualquier operación fuera de una lista cerrada ANTES de construir la solicitud.

Los costos son REALES (Cost Explorer por recurso, ventana de 14 días; ver `aws_costs.py`) cuando AWS_COST_EXPLORER_RESOURCES=true
(valor por defecto) y la cuenta tiene habilitado Cost Explorer a nivel de recurso. Si Cost Explorer no está disponible,
cada recurso conserva la estimación por tabla de precios y se marca `cost_source="estimate"`; el escaneo avisa pero no falla.
Además se recoge el costo de la CUENTA por servicio, región y período (diario el último mes, mensual el historial).

Cada error de AWS se clasifica (permisos, Cost Explorer no activado, límite de solicitudes, datos no disponibles...) y se informa con
una indicación de qué hacer; nunca se guarda el texto original de la excepción.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from statistics import fmean
from typing import Any, Callable

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import DAYS_PER_MONTH, instance_monthly_cost, snapshot_monthly_cost, volume_monthly_cost
from ..secrets import SecretResolver
from . import aws_costs, aws_errors, aws_guard
from .base import CollectionResult, CostRecord

log = logging.getLogger(__name__)
WINDOW_DAYS = 14
MAX_CLOUDTRAIL_LOOKUPS = 50
USER_AGENT_EXTRA = "cloudcost-optimizer/readonly"         # permite al cliente filtrar en CloudTrail lo que hace la plataforma


class AwsAccessError(PermissionError):
    """No se pudo acceder a la cuenta (rol no asumible, credenciales, cuenta distinta a la registrada). No se reintenta."""

    def __init__(self, message: str, issue: aws_errors.AwsIssue | None = None):
        super().__init__(message)
        self.issue = issue


def _tags(raw: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in (raw or []) if "Key" in t}


def _age_days(ts: datetime | None) -> int | None:
    return max(0, (datetime.now(timezone.utc) - ts).days) if ts else None


class AwsCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, use_cost_explorer: bool = False,
                 on_api_error=None, session=None, cost_tag_key: str | None = None, cost_history_months: int = 6,
                 cost_metric: str = aws_costs.DEFAULT_METRIC, ce_request_budget: int = aws_costs.DEFAULT_CE_BUDGET,
                 sleep: Callable[[float], None] = time.sleep, today: Callable[[], date] = date.today,
                 retry_attempts: int = 4):
        self.account = account
        self.secrets = secrets
        self.use_cost_explorer = use_cost_explorer
        self.cost_tag_key = (cost_tag_key or "").strip() or None
        self.cost_history_months = cost_history_months
        self.cost_metric = cost_metric
        self.on_api_error = on_api_error or (lambda api: None)
        self._session = session
        self._sleep = sleep
        self._today = today
        self._retry_attempts = retry_attempts
        self.budget = aws_costs.CostExplorerBudget(ce_request_budget)
        self.warnings: list[str] = []
        self.issues: list[dict[str, Any]] = []
        self.data_quality: dict[str, Any] = {}
        self.identity_verified = False                  # True cuando GetCallerIdentity confirmó que las credenciales son de la cuenta registrada
        self.partial = False

    # ------------------------------------------------------------------ sesión
    def _boto_session(self):
        if self._session is None:
            self._session = self._build_session()
        aws_guard.install(self._session)                 # idempotente; antes de crear ningún cliente
        return self._session

    def _build_session(self):
        import boto3

        role_arn = self.account.get("role_arn")
        if not role_arn:
            return boto3.Session()                       # cadena de credenciales por defecto (solo desarrollo)
        external_id = self.secrets.resolve(self.account.get("external_id_ref"), self.account.get("organization_id"))
        params: dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": "cloudcost-optimizer", "DurationSeconds": 3600}
        if external_id:
            params["ExternalId"] = external_id
        try:
            creds, _ = aws_errors.call_with_retry(lambda: boto3.client("sts").assume_role(**params)["Credentials"],
                                                  max_attempts=self._retry_attempts, sleep=self._sleep)
        except Exception as exc:
            issue = aws_errors.issue_from("sts:AssumeRole", exc, attempts=getattr(exc, "cloudcost_attempts", 1))
            self.on_api_error("sts:AssumeRole")
            raise AwsAccessError("No se pudo asumir el rol de solo lectura. " + issue.message(), issue) from None
        return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                             aws_session_token=creds["SessionToken"])

    def _client(self, service: str, region: str):
        try:
            from botocore.config import Config

            cfg = Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=30,
                         user_agent_extra=USER_AGENT_EXTRA)
            return self._boto_session().client(service, region_name=region, config=cfg)
        except ImportError:                              # pragma: no cover — botocore siempre viene con boto3
            return self._boto_session().client(service, region_name=region)

    def _check_identity(self) -> None:
        """La cuenta a la que apuntan las credenciales debe ser la registrada: evita mezclar costos de cuentas distintas."""
        expected = str(self.account.get("account_ref") or "")
        if not (expected.isdigit() and len(expected) == 12):
            return
        try:
            who, _ = aws_errors.call_with_retry(lambda: self._client("sts", "us-east-1").get_caller_identity(),
                                                max_attempts=self._retry_attempts, sleep=self._sleep)
        except AwsAccessError:                           # la sesión no se pudo construir (AssumeRole): ya viene clasificada
            raise
        except Exception as exc:
            issue = aws_errors.issue_from("sts:GetCallerIdentity", exc, attempts=getattr(exc, "cloudcost_attempts", 1))
            self.on_api_error("sts:GetCallerIdentity")
            raise AwsAccessError("No se pudo verificar la identidad de la cuenta. " + issue.message(), issue) from None
        actual = str(who.get("Account", ""))
        self.identity_verified = actual == expected
        if actual != expected:
            self.on_api_error("sts:GetCallerIdentity")
            raise AwsAccessError(f"Las credenciales pertenecen a la cuenta ****{actual[-4:]} y la cuenta registrada es "
                                 f"****{expected[-4:]}: no se recoge nada para no mezclar costos de cuentas distintas.")

    def _safe(self, api: str, fn, default, *, inventory: bool = True, region: str | None = None, retry: bool = True,
              detail: str = ""):
        """`inventory=False`: el fallo no afecta a lo inventariado (p. ej. Cost Explorer), solo avisa; no marca el escaneo parcial.

        Los límites de solicitudes se reintentan con espera exponencial; el resto de errores se clasifican y se informan una vez.
        """
        try:
            if not retry:                              # el llamador ya reintenta página a página (Cost Explorer)
                return fn()
            value, _ = aws_errors.call_with_retry(fn, max_attempts=self._retry_attempts, sleep=self._sleep)
            return value
        except Exception as exc:                      # los fallos parciales no deben abortar todo el escaneo
            if isinstance(exc, aws_costs.CostExplorerBudgetExceeded):
                self.warnings.append(f"{api}: {exc}")
                self.issues.append({"api": api, "kind": "budget", "code": "CostExplorerBudgetExceeded", "retryable": False,
                                    "hint": "Se alcanzó el máximo de solicitudes a Cost Explorer por escaneo.", "attempts": 0})
                return default
            issue = aws_errors.issue_from(api + detail, exc, attempts=getattr(exc, "cloudcost_attempts", 1), region=region)
            self.on_api_error(api)               # etiqueta de métrica de cardinalidad baja: solo el nombre de la API
            if inventory:
                self.partial = True
            self.issues.append(issue.to_dict())
            self.warnings.append(issue.message())
            log.warning("aws api error api=%s kind=%s code=%s", api, issue.kind, issue.code)
            return default

    # ------------------------------------------------------------------ colección
    def collect(self) -> CollectionResult:
        result = CollectionResult()
        self._check_identity()
        for region in self.account.get("regions") or ["us-east-1"]:
            result.resources += self._collect_region(region)
        now_day = self._today()
        by_id = {r.resource_id: r for r in result.resources}
        if self.use_cost_explorer:
            self._apply_real_costs(by_id, result)
            self._collect_account_costs(result)
        self._apply_tag_history(result.resources)
        for res in result.resources:
            if res.cost_source != "cost_explorer":
                result.costs += [CostRecord(res.resource_id, now_day - timedelta(days=d),
                                            round(res.monthly_cost / DAYS_PER_MONTH, 4), res.service, res.region, "estimate")
                                 for d in range(1, WINDOW_DAYS + 1)]
        if self.use_cost_explorer:
            self.data_quality.setdefault("cost_explorer", {})["requests"] = self.budget.used
            self.data_quality["cost_explorer"]["request_budget"] = self.budget.limit
        result.warnings = self.warnings
        result.issues = self.issues
        result.data_quality = self.data_quality
        result.partial = self.partial
        return result

    def _collect_region(self, region: str) -> list[NormalizedResource]:
        ec2 = self._client("ec2", region)
        cw = self._client("cloudwatch", region)
        ct = self._client("cloudtrail", region)
        out: list[NormalizedResource] = []
        out += self._instances(ec2, cw, region)
        out += self._volumes(ec2, ct, region)
        out += self._snapshots(ec2, region)
        return out

    # ------------------------------------------------------------------ EC2
    def _instances(self, ec2, cw, region) -> list[NormalizedResource]:
        def fetch():
            rows = []
            for page in ec2.get_paginator("describe_instances").paginate(
                    Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]):
                for resv in page["Reservations"]:
                    rows += resv["Instances"]
            return rows

        instances = self._safe("ec2:DescribeInstances", fetch, [], region=region)
        running = [i["InstanceId"] for i in instances if i["State"]["Name"] == "running"]
        metrics = self._instance_metrics(cw, running) if running else {}
        out = []
        for i in instances:
            iid, tags = i["InstanceId"], _tags(i.get("Tags"))
            m = metrics.get(iid, {})
            age = _age_days(i.get("LaunchTime"))
            observed = min(WINDOW_DAYS, m.get("hours_with_data", 0) // 24, age if age is not None else WINDOW_DAYS)
            out.append(NormalizedResource(
                "aws", "compute", "ec2", iid, region, name=tags.get("Name"), environment=environment_from_tags(tags),
                instance_type=i.get("InstanceType"), state=i["State"]["Name"],
                monthly_cost=instance_monthly_cost(i.get("InstanceType")) or 0.0, cost_source="estimate",
                cpu_avg=m.get("cpu_avg"), cpu_max=m.get("cpu_max"), memory_avg=m.get("memory_avg"),
                age_days=age, observation_days=int(observed), tags=tags))
        return out

    def _instance_metrics(self, cw, instance_ids: list[str]) -> dict[str, dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=WINDOW_DAYS)
        mem_dims = self._memory_dimensions(cw)
        result: dict[str, dict[str, Any]] = {iid: {} for iid in instance_ids}
        for chunk_start in range(0, len(instance_ids), 100):
            chunk = instance_ids[chunk_start: chunk_start + 100]
            queries, index = [], {}
            for n, iid in enumerate(chunk):
                dims = [{"Name": "InstanceId", "Value": iid}]
                for suffix, stat in (("a", "Average"), ("x", "Maximum")):
                    qid = f"c{n}{suffix}"
                    index[qid] = (iid, "cpu_avg" if suffix == "a" else "cpu_max")
                    queries.append({"Id": qid, "ReturnData": True, "MetricStat": {
                        "Metric": {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization", "Dimensions": dims},
                        "Period": 3600, "Stat": stat}})
                if iid in mem_dims:
                    qid = f"m{n}"
                    index[qid] = (iid, "memory_avg")
                    queries.append({"Id": qid, "ReturnData": True, "MetricStat": {
                        "Metric": {"Namespace": "CWAgent", "MetricName": "mem_used_percent", "Dimensions": mem_dims[iid]},
                        "Period": 3600, "Stat": "Average"}})

            def fetch(queries=queries):
                values: dict[str, list[float]] = {}
                token = None
                while True:
                    kw = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
                    if token:
                        kw["NextToken"] = token
                    resp = cw.get_metric_data(**kw)
                    for r in resp["MetricDataResults"]:
                        values.setdefault(r["Id"], []).extend(r["Values"])
                    token = resp.get("NextToken")
                    if not token:
                        return values

            for qid, vals in self._safe("cloudwatch:GetMetricData", fetch, {}).items():
                iid, key = index[qid]
                if not vals:
                    continue
                result[iid][key] = round(max(vals) if key == "cpu_max" else fmean(vals), 2)
                if key == "cpu_avg":
                    result[iid]["hours_with_data"] = len(vals)
        return result

    def _memory_dimensions(self, cw) -> dict[str, list[dict[str, str]]]:
        """Memoria vía CloudWatch Agent (CWAgent/mem_used_percent); descubre el conjunto exacto de dimensiones."""
        def fetch():
            found: dict[str, list[dict[str, str]]] = {}
            for page in cw.get_paginator("list_metrics").paginate(Namespace="CWAgent", MetricName="mem_used_percent"):
                for metric in page["Metrics"]:
                    dims = metric["Dimensions"]
                    iid = next((d["Value"] for d in dims if d["Name"] == "InstanceId"), None)
                    if iid and (iid not in found or len(dims) < len(found[iid])):
                        found[iid] = dims
            return found
        return self._safe("cloudwatch:ListMetrics", fetch, {})

    # ------------------------------------------------------------------ EBS
    def _volumes(self, ec2, ct, region) -> list[NormalizedResource]:
        def fetch():
            rows = []
            for page in ec2.get_paginator("describe_volumes").paginate():
                rows += page["Volumes"]
            return rows

        out, lookups = [], 0
        for v in self._safe("ec2:DescribeVolumes", fetch, [], region=region):
            tags, vid = _tags(v.get("Tags")), v["VolumeId"]
            attached = bool(v.get("Attachments"))
            unattached_days = None
            if v["State"] == "available" and lookups < MAX_CLOUDTRAIL_LOOKUPS:
                lookups += 1
                unattached_days = self._days_since_detach(ct, vid)
                self._sleep(0.55)                                 # LookupEvents: ~2 req/s
            out.append(NormalizedResource(
                "aws", "storage", "ebs", vid, region, name=tags.get("Name"), environment=environment_from_tags(tags),
                volume_type=v.get("VolumeType"), state=v["State"], attached=attached, size_gb=float(v["Size"]),
                monthly_cost=volume_monthly_cost(v.get("VolumeType"), v["Size"]), cost_source="estimate",
                age_days=_age_days(v.get("CreateTime")), unattached_days=unattached_days,
                observation_days=WINDOW_DAYS, tags=tags))
        return out

    def _days_since_detach(self, ct, volume_id: str) -> int | None:
        """Último DetachVolume en CloudTrail (90 días). None => lo calcula el servicio con su propio historial."""
        def fetch():
            events = ct.lookup_events(LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": volume_id}],
                                      StartTime=datetime.now(timezone.utc) - timedelta(days=90), MaxResults=50)["Events"]
            times = [e["EventTime"] for e in events if e.get("EventName") == "DetachVolume"]
            return _age_days(max(times)) if times else None
        return self._safe("cloudtrail:LookupEvents", fetch, None)

    # ------------------------------------------------------------------ Snapshots
    def _snapshots(self, ec2, region) -> list[NormalizedResource]:
        def fetch_snaps():
            rows = []
            for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
                rows += page["Snapshots"]
            return rows

        def fetch_amis():
            mapping: dict[str, list[str]] = {}
            for img in ec2.describe_images(Owners=["self"])["Images"]:
                for bdm in img.get("BlockDeviceMappings", []):
                    sid = (bdm.get("Ebs") or {}).get("SnapshotId")
                    if sid:
                        mapping.setdefault(sid, []).append(img["ImageId"])
            return mapping

        amis = self._safe("ec2:DescribeImages", fetch_amis, {}, region=region)
        out = []
        for sn in self._safe("ec2:DescribeSnapshots", fetch_snaps, [], region=region):
            tags, sid = _tags(sn.get("Tags")), sn["SnapshotId"]
            desc = sn.get("Description") or ""
            managed = ("aws-backup" if "aws:backup:source-resource" in tags or desc.startswith("Created by AWS Backup")
                       else "dlm" if "aws:dlm:lifecycle-policy-id" in tags else None)
            attrs: dict[str, Any] = {}
            if managed:
                attrs["managed_by"] = managed
            if sid in amis:
                attrs["ami_ids"] = amis[sid]
            out.append(NormalizedResource(
                "aws", "storage", "ebs_snapshot", sid, region, name=tags.get("Name"),
                environment=environment_from_tags(tags), state=sn.get("State"), size_gb=float(sn["VolumeSize"]),
                monthly_cost=snapshot_monthly_cost(sn["VolumeSize"]), cost_source="estimate_upper_bound",
                age_days=_age_days(sn.get("StartTime")), tags=tags, attributes=attrs))
        return out

    # ------------------------------------------------------------------ Costos reales (Cost Explorer)
    def _ce(self):
        return self._client("ce", "us-east-1")                   # Cost Explorer es un endpoint global

    def _ce_retry(self, call):
        value, _ = aws_errors.call_with_retry(call, max_attempts=self._retry_attempts, sleep=self._sleep)
        return value

    def _apply_real_costs(self, by_id: dict[str, NormalizedResource], result: CollectionResult) -> None:
        """Sustituye la estimación por el costo real de los últimos 14 días, recurso por recurso."""
        today = self._today()
        real = self._safe("ce:GetCostAndUsageWithResources",
                          lambda: aws_costs.fetch_resource_costs(self._ce(), today=today, metric=self.cost_metric,
                                                                 budget=self.budget, retry=self._ce_retry),
                          None, inventory=False, retry=False)
        quality: dict[str, Any] = self.data_quality.setdefault("cost_explorer", {})
        if real is None:                                  # fallo ya registrado por _safe: se conserva la estimación
            quality["resource_costs"] = {"available": False}
            return
        window_start, window_end = today - timedelta(days=aws_costs.RESOURCE_WINDOW_DAYS), today
        matched, flagged = 0, 0
        for rid, cost in real.items():
            res = by_id.get(rid)
            if not res or not cost.daily:
                continue
            matched += 1
            expected = min(aws_costs.RESOURCE_WINDOW_DAYS, res.age_days + 1 if res.age_days is not None else aws_costs.RESOURCE_WINDOW_DAYS)
            flags = cost.quality_flags(expected_days=expected)
            flagged += bool(flags)
            res.monthly_cost = cost.monthly_estimate(DAYS_PER_MONTH, age_days=res.age_days)
            res.cost_source = "cost_explorer"
            res.attributes["cost_basis"] = {
                "source": "cost_explorer", "metric": self.cost_metric, "window_days": aws_costs.RESOURCE_WINDOW_DAYS,
                "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
                "days_with_data": cost.days_with_data, "last_14d_total": round(sum(a for _, a in cost.daily), 2),
                "quality_flags": flags, "as_of": today.isoformat()}
            result.costs += [CostRecord(rid, d, a, res.service, res.region, "cost_explorer") for d, a in cost.daily]
        billable = [r for r in by_id.values() if r.service in ("ec2", "ebs", "ebs_snapshot")]
        orphans = {rid: c for rid, c in real.items() if rid not in by_id}
        quality["resource_costs"] = {
            "available": True, "matched": matched, "with_quality_flags": flagged,
            "inventory_without_cost": sum(1 for r in billable if r.cost_source != "cost_explorer"),
            "cost_without_inventory": len(orphans),
            "cost_without_inventory_usd_14d": round(sum(a for c in orphans.values() for _, a in c.daily), 2),
            "window": [window_start.isoformat(), window_end.isoformat()]}
        if orphans:
            self.warnings.append(f"Cost Explorer devolvió costos de {len(orphans)} recursos que no están en el inventario "
                                 "(eliminados, de otra región no escaneada o de otro tipo): no se usan para calcular ahorros")
        if real and not matched:
            self.warnings.append("Cost Explorer devolvió costos pero ninguno coincide con los recursos inventariados")
        elif by_id and not real:
            self.warnings.append("Cost Explorer sin datos por recurso: se usa la tabla de precios (¿habilitado el nivel de recurso?)")

    def _collect_account_costs(self, result: CollectionResult) -> None:
        """Costo de la cuenta por servicio y región: DIARIO el último mes y MENSUAL el historial. Una sola moneda por fila."""
        today = self._today()
        ref = str(self.account.get("account_ref") or "")
        quality: dict[str, Any] = self.data_quality.setdefault("cost_explorer", {})
        if not (ref.isdigit() and len(ref) == 12):
            self.warnings.append("account_ref no es un ID de cuenta de 12 dígitos: los costos por cuenta podrían incluir otras cuentas vinculadas")
        daily = self._safe(
            "ce:GetCostAndUsage",
            lambda: aws_costs.fetch_account_costs(self._ce(), ref, granularity="DAILY",
                                                  start=today - timedelta(days=aws_costs.ACCOUNT_DAILY_DAYS), end=today,
                                                  metric=self.cost_metric, budget=self.budget, retry=self._ce_retry),
            None, inventory=False, retry=False, detail="[cuenta, diario]")
        months = max(1, min(self.cost_history_months, aws_costs.MAX_HISTORY_MONTHS))
        monthly = self._safe(
            "ce:GetCostAndUsage",
            lambda: aws_costs.fetch_account_costs(self._ce(), ref, granularity="MONTHLY",
                                                  start=aws_costs.month_start(today, months), end=aws_costs.month_start(today),
                                                  metric=self.cost_metric, budget=self.budget, retry=self._ce_retry),
            None, inventory=False, retry=False, detail="[cuenta, mensual]")
        rows = (daily or []) + (monthly or [])
        result.account_costs += rows
        currencies = sorted({r.currency for r in rows})
        quality["account_costs"] = {
            "daily_available": daily is not None, "monthly_available": monthly is not None, "rows": len(rows),
            "currencies": currencies, "estimated_rows": sum(1 for r in rows if r.estimated),
            "negative_rows": sum(1 for r in rows if r.amount < 0),
            "daily_days_with_data": len({r.period_start for r in (daily or [])}),
            "daily_days_expected": aws_costs.ACCOUNT_DAILY_DAYS}
        if len(currencies) > 1:
            self.warnings.append(f"Cost Explorer devolvió varias monedas ({', '.join(currencies)}): no se suman entre sí")
        if daily is not None and not daily:
            self.warnings.append("Cost Explorer no devolvió costos diarios de la cuenta (¿cuenta nueva o sin gasto en el período?)")

    def _apply_tag_history(self, resources: list[NormalizedResource]) -> None:
        """Historial mensual (hasta 12 meses) por valor de la etiqueta de asignación de costos configurada."""
        if not (self.use_cost_explorer and self.cost_tag_key):
            return
        history = self._safe(
            "ce:GetCostAndUsage",
            lambda: aws_costs.fetch_tag_history(self._ce(), self.cost_tag_key, months=self.cost_history_months,
                                                today=self._today(), metric=self.cost_metric, budget=self.budget,
                                                retry=self._ce_retry), {}, inventory=False, retry=False, detail="[etiqueta]")
        for res in resources:
            value = res.tags.get(self.cost_tag_key)
            series = history.get(value) if value else None
            if series:
                res.attributes["cost_history"] = {
                    "scope": "tag", "tag_key": self.cost_tag_key, "tag_value": value, "monthly": series,
                    "trend_pct": aws_costs.trend_pct(series),
                    "note": "Costo agregado de todos los recursos con esta etiqueta, no de este recurso por separado."}
