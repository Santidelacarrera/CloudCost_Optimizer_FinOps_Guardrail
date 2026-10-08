"""Conector AWS de SOLO LECTURA: inventario EC2/EBS/snapshots + métricas de CloudWatch.

Permisos necesarios: ver infrastructure/terraform/aws-readonly-role. El conector nunca llama a APIs de escritura.
El costo de cada recurso viene de Cost Explorer (por ID/ARN y, si no hay datos a nivel de recurso, por la etiqueta de
asignación de costos de la cuenta: ``settings.cost_allocation_tag``). Solo cae a una estimación por tabla de precios si Cost Explorer
no devuelve nada; el campo ``cost_source`` lo deja explícito y las reglas pueden exigir costo real antes de proponer cambios.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from statistics import fmean
from typing import Any

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import (
    DAYS_PER_MONTH,
    instance_monthly_cost,
    rds_monthly_cost,
    snapshot_monthly_cost,
    volume_monthly_cost,
)
from ..secrets import SecretResolver
from .aws_cost import REAL_COST_SOURCES, CostExplorerSource, attribute_costs, monthly_from_series
from .base import CollectionResult, CostRecord

log = logging.getLogger(__name__)
WINDOW_DAYS = 14
MAX_CLOUDTRAIL_LOOKUPS = 50


def _tags(raw: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in (raw or []) if "Key" in t}


def _age_days(ts: datetime | None) -> int | None:
    return max(0, (datetime.now(timezone.utc) - ts).days) if ts else None


class AwsCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, use_cost_explorer: bool = True,
                 on_api_error=None, session=None):
        self.account = account
        self.secrets = secrets
        self.use_cost_explorer = use_cost_explorer
        self.on_api_error = on_api_error or (lambda api: None)
        self._session = session
        self.warnings: list[str] = []
        self.partial = False

    # ------------------------------------------------------------------ sesión
    def _boto_session(self):
        if self._session is not None:
            return self._session
        import boto3

        role_arn = self.account.get("role_arn")
        if not role_arn:
            self._session = boto3.Session()          # cadena de credenciales por defecto (solo desarrollo)
            return self._session
        external_id = self.secrets.resolve(self.account.get("external_id_ref"))
        params: dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": "cloudcost-optimizer", "DurationSeconds": 3600}
        if external_id:
            params["ExternalId"] = external_id
        creds = boto3.client("sts").assume_role(**params)["Credentials"]
        self._session = boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                      aws_secret_access_key=creds["SecretAccessKey"],
                                      aws_session_token=creds["SessionToken"])
        return self._session

    def _safe(self, api: str, fn, default):
        try:
            return fn()
        except Exception as exc:                      # los fallos parciales no deben abortar todo el escaneo
            self.on_api_error(api)
            self.partial = True
            self.warnings.append(f"{api}: {type(exc).__name__}")
            log.warning("aws api error api=%s err=%s", api, type(exc).__name__)
            return default

    # ------------------------------------------------------------------ colección
    def collect(self) -> CollectionResult:
        result = CollectionResult()
        for region in self.account.get("regions") or ["us-east-1"]:
            result.resources += self._collect_region(region)
        now_day = date.today()
        if self.use_cost_explorer and result.resources:
            self._apply_real_costs(result)
        for res in result.resources:
            if res.cost_source not in REAL_COST_SOURCES:
                result.costs += [CostRecord(res.resource_id, now_day - timedelta(days=d),
                                            round(res.monthly_cost / DAYS_PER_MONTH, 4), res.service, res.region, "estimate")
                                 for d in range(1, WINDOW_DAYS + 1)]
        result.warnings = self.warnings
        result.partial = self.partial
        return result

    def _collect_region(self, region: str) -> list[NormalizedResource]:
        s = self._boto_session()
        ec2 = s.client("ec2", region_name=region)
        cw = s.client("cloudwatch", region_name=region)
        ct = s.client("cloudtrail", region_name=region)
        out: list[NormalizedResource] = []
        out += self._instances(ec2, cw, region)
        out += self._volumes(ec2, ct, region)
        out += self._snapshots(ec2, region)
        out += self._databases(s.client("rds", region_name=region), cw, region)
        return out

    # ------------------------------------------------------------------ RDS
    def _databases(self, rds, cw, region) -> list[NormalizedResource]:
        def fetch():
            rows = []
            for page in rds.get_paginator("describe_db_instances").paginate():
                rows += page["DBInstances"]
            return rows

        dbs = self._safe("rds:DescribeDBInstances", fetch, [])
        available = [d["DBInstanceIdentifier"] for d in dbs if d.get("DBInstanceStatus") == "available"]
        metrics = self._db_metrics(cw, available) if available else {}
        out = []
        for d in dbs:
            ident, tags = d["DBInstanceIdentifier"], _tags(d.get("TagList"))
            m = metrics.get(ident, {})
            multi_az = bool(d.get("MultiAZ"))
            age = _age_days(d.get("InstanceCreateTime"))
            observed = min(WINDOW_DAYS, m.get("hours_with_data", 0) // 24, age if age is not None else WINDOW_DAYS)
            out.append(NormalizedResource(
                "aws", "database", "rds", ident, region, name=tags.get("Name") or ident,
                environment=environment_from_tags(tags), instance_type=d.get("DBInstanceClass"),
                state=d.get("DBInstanceStatus"), size_gb=float(d.get("AllocatedStorage") or 0),
                monthly_cost=rds_monthly_cost(d.get("DBInstanceClass"), d.get("AllocatedStorage"), multi_az),
                cost_source="estimate", cpu_avg=m.get("cpu_avg"), cpu_max=m.get("cpu_max"), age_days=age,
                observation_days=int(observed), tags=tags,
                attributes={"engine": d.get("Engine"), "multi_az": multi_az, "arn": d.get("DBInstanceArn"),
                            "cluster_id": d.get("DBClusterIdentifier"),
                            **{k: m[k] for k in ("connections_max", "connections_avg") if k in m}}))
        return out

    def _db_metrics(self, cw, identifiers: list[str]) -> dict[str, dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=WINDOW_DAYS)
        result: dict[str, dict[str, Any]] = {i: {} for i in identifiers}
        for chunk_start in range(0, len(identifiers), 60):                 # 3 consultas por base, máx. 500 por llamada
            chunk = identifiers[chunk_start: chunk_start + 60]
            queries, index = [], {}
            for n, ident in enumerate(chunk):
                dims = [{"Name": "DBInstanceIdentifier", "Value": ident}]
                for qid, metric, stat, key in ((f"c{n}", "CPUUtilization", "Average", "cpu_avg"),
                                               (f"x{n}", "DatabaseConnections", "Maximum", "connections_max"),
                                               (f"a{n}", "DatabaseConnections", "Average", "connections_avg")):
                    index[qid] = (ident, key)
                    queries.append({"Id": qid, "ReturnData": True, "MetricStat": {
                        "Metric": {"Namespace": "AWS/RDS", "MetricName": metric, "Dimensions": dims},
                        "Period": 3600, "Stat": stat}})

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
                ident, key = index[qid]
                if not vals:
                    continue
                result[ident][key] = round(max(vals) if key == "connections_max" else fmean(vals), 2)
                if key == "cpu_avg":
                    result[ident]["hours_with_data"] = len(vals)
                    result[ident]["cpu_max"] = round(max(vals), 2)         # aproximación: máximo de promedios horarios
        return result

    # ------------------------------------------------------------------ EC2
    def _instances(self, ec2, cw, region) -> list[NormalizedResource]:
        def fetch():
            rows = []
            for page in ec2.get_paginator("describe_instances").paginate(
                    Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]):
                for resv in page["Reservations"]:
                    rows += resv["Instances"]
            return rows

        instances = self._safe("ec2:DescribeInstances", fetch, [])
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
        for v in self._safe("ec2:DescribeVolumes", fetch, []):
            tags, vid = _tags(v.get("Tags")), v["VolumeId"]
            attached = bool(v.get("Attachments"))
            unattached_days = None
            if v["State"] == "available" and lookups < MAX_CLOUDTRAIL_LOOKUPS:
                lookups += 1
                unattached_days = self._days_since_detach(ct, vid)
                time.sleep(0.55)                                  # LookupEvents: ~2 req/s
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

        amis = self._safe("ec2:DescribeImages", fetch_amis, {})
        out = []
        for sn in self._safe("ec2:DescribeSnapshots", fetch_snaps, []):
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
    def _apply_real_costs(self, result: CollectionResult) -> None:
        """Sustituye la estimación por el costo facturado. Un fallo de Cost Explorer NO marca el inventario como parcial."""
        def on_error(api: str, exc: Exception) -> None:
            self.on_api_error(api)
            log.warning("aws api error api=%s err=%s", api, type(exc).__name__)

        source = CostExplorerSource(self._boto_session(), on_error=on_error)
        attr = attribute_costs(result.resources, source, tag_key=(self.account.get("settings") or {}).get("cost_allocation_tag"))
        self.warnings += attr.warnings
        by_id = {r.resource_id: r for r in result.resources}
        for rid, series in attr.by_resource.items():
            res = by_id[rid]
            res.monthly_cost = monthly_from_series(series)
            res.cost_source = attr.source[rid]
            res.attributes["cost_window_days"] = len(series)
            result.costs += [CostRecord(rid, d, a, res.service, res.region, attr.source[rid]) for d, a in series]
        estimated = sum(1 for r in result.resources if r.cost_source not in REAL_COST_SOURCES)
        if estimated:
            self.warnings.append(f"{estimated} recursos sin costo real en Cost Explorer: se usa estimación por tabla de precios")
