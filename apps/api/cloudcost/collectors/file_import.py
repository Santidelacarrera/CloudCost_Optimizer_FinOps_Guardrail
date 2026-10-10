"""Importación de archivos CSV (inventario + uso + costo) como origen de datos. Solo lectura de lo que el usuario sube.

Formato: una fila por recurso. Se aceptan cabeceras en inglés o español. Excel: Guardar como → CSV UTF-8.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from ..domain.k8s import DEFAULT_CPU_CORE_HOUR, DEFAULT_MEM_GIB_HOUR, QuantityError, parse_cpu, parse_memory, reserved_monthly_cost
from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import DAYS_PER_MONTH, instance_monthly_cost, rds_monthly_cost, snapshot_monthly_cost, volume_monthly_cost
from .base import CollectionResult, CostRecord

MAX_ROWS = 5000
MAX_BYTES = 2_000_000
# servicio -> (proveedor, tipo de recurso). Todos tienen reglas de recomendación; los servicios sin reglas (S3, Lambda…) no se importan.
SERVICE_INFO = {
    "ec2": ("aws", "compute"), "ebs": ("aws", "storage"), "ebs_snapshot": ("aws", "storage"), "rds": ("aws", "database"),
    "vm": ("azure", "compute"), "disk": ("azure", "storage"), "disk_snapshot": ("azure", "storage"),
    "gce": ("gcp", "compute"), "pd": ("gcp", "storage"), "pd_snapshot": ("gcp", "storage"),
    "k8s_workload": ("kubernetes", "k8s_workload"),
}
SERVICES = set(SERVICE_INFO)
COMPUTE = {"ec2", "vm", "gce"}
VOLUMES = {"ebs", "disk", "pd"}
SNAPSHOTS = {"ebs_snapshot", "disk_snapshot", "pd_snapshot"}
_SERVICE_ALIASES = {"snapshot": "ebs_snapshot", "ebs-snapshot": "ebs_snapshot", "instancia": "ec2", "volumen": "ebs",
                    "volume": "ebs", "instance": "ec2", "base_de_datos": "rds", "db": "rds", "azure_vm": "vm", "maquina_virtual": "vm",
                    "managed_disk": "disk", "disco": "disk", "azure_disk": "disk", "azure_snapshot": "disk_snapshot",
                    "gcp_vm": "gce", "compute_engine": "gce", "persistent_disk": "pd", "gcp_disk": "pd", "gcp_snapshot": "pd_snapshot",
                    "kubernetes": "k8s_workload", "k8s": "k8s_workload", "workload": "k8s_workload", "carga": "k8s_workload"}
_ID = re.compile(r"^[A-Za-z0-9._:/@+=-]{1,128}$")

TEMPLATE_HEADER = ["resource_id", "service", "name", "environment", "region", "instance_type", "volume_type", "size_gb",
                   "attached", "monthly_cost", "cpu_avg", "cpu_max", "memory_avg", "age_days", "observation_days",
                   "unattached_days", "tags"]
TEMPLATE_CSV = (
    ",".join(TEMPLATE_HEADER) + "\n"
    "i-0aaa111,ec2,web-prod-1,production,us-east-1,m5.2xlarge,,,,,12.4,41,19.8,400,30,,Name=web-prod-1;Environment=production\n"
    "vol-0bbb222,ebs,legacy-data,production,us-east-1,,gp2,500,false,,,,,300,30,45,Name=legacy-data;Environment=production\n"
    "snap-0ccc333,ebs_snapshot,old-backup,development,us-east-1,,,300,,,,,,210,,,Name=old-backup;Environment=development\n"
)

EXTRA_HEADER = ["engine", "multi_az", "connections_avg", "connections_max", "deletion_protection", "backup_retention_days", "read_replicas",
                "cluster", "namespace", "workload", "kind", "container", "replicas", "cpu_request", "mem_request", "cpu_limit", "mem_limit",
                "cpu_p95_cores", "cpu_max_cores", "cpu_avg_cores", "mem_max", "hpa", "oom_killed"]
_FULL_SAMPLES = [
    {"resource_id": "i-0aaa111", "service": "ec2", "name": "web-prod-1", "environment": "production", "region": "us-east-1", "instance_type": "m5.2xlarge",
     "cpu_avg": 12.4, "cpu_max": 41, "memory_avg": 19.8, "age_days": 400, "observation_days": 30, "tags": "Name=web-prod-1;Environment=production"},
    {"resource_id": "db-orders", "service": "rds", "name": "orders-db", "environment": "development", "region": "us-east-1",
     "instance_type": "db.m5.large", "size_gb": 200, "cpu_avg": 0.5, "age_days": 400, "observation_days": 30, "engine": "postgres",
     "multi_az": "false", "connections_avg": 0, "connections_max": 0, "deletion_protection": "false", "backup_retention_days": 7},
    {"resource_id": "/subscriptions/s1/virtualMachines/batch-vm", "service": "vm", "name": "batch-vm", "environment": "development", "region": "westeurope",
     "instance_type": "Standard_D4s_v3", "monthly_cost": 180, "cpu_avg": 3.0, "cpu_max": 12, "memory_avg": 20, "age_days": 300, "observation_days": 30},
    {"resource_id": "disk-azure-1", "service": "disk", "name": "old-disk", "environment": "development", "region": "westeurope", "volume_type": "premium_lrs",
     "size_gb": 256, "attached": "false", "age_days": 300, "observation_days": 30, "unattached_days": 45},
    {"resource_id": "pd-gcp-1", "service": "pd", "name": "old-pd", "environment": "development", "region": "us-central1", "volume_type": "pd-ssd",
     "size_gb": 100, "attached": "false", "age_days": 200, "observation_days": 30, "unattached_days": 60},
    {"service": "k8s_workload", "environment": "production", "cluster": "minikube", "namespace": "shop", "workload": "api", "kind": "Deployment",
     "container": "app", "replicas": 3, "cpu_request": "500m", "mem_request": "512Mi", "cpu_limit": "500m", "mem_limit": "512Mi",
     "cpu_avg_cores": 0.01, "cpu_p95_cores": 0.03, "cpu_max_cores": 0.10, "mem_max": "60Mi", "hpa": "false", "oom_killed": "false", "observation_days": 14},
]


def _full_template() -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TEMPLATE_HEADER + EXTRA_HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(_FULL_SAMPLES)
    return buf.getvalue()


TEMPLATE_FULL_CSV = _full_template()

_ALIASES = {
    "id": "resource_id", "recurso": "resource_id", "id_recurso": "resource_id", "servicio": "service", "tipo": "service",
    "nombre": "name", "entorno": "environment", "ambiente": "environment", "region": "region", "región": "region",
    "tipo_instancia": "instance_type", "tipo_volumen": "volume_type", "tamano_gb": "size_gb", "tamaño_gb": "size_gb",
    "adjunto": "attached", "costo_mensual": "monthly_cost", "costo": "monthly_cost", "cpu_promedio": "cpu_avg",
    "cpu_maxima": "cpu_max", "cpu_máxima": "cpu_max", "memoria_promedio": "memory_avg", "edad_dias": "age_days",
    "edad_días": "age_days", "dias_observacion": "observation_days", "días_observación": "observation_days",
    "dias_sin_adjuntar": "unattached_days", "etiquetas": "tags",
    "motor": "engine", "clase_db": "instance_type", "conexiones_promedio": "connections_avg", "conexiones_maximas": "connections_max",
    "proteccion_borrado": "deletion_protection", "retencion_backup_dias": "backup_retention_days", "replicas_lectura": "read_replicas",
    "clúster": "cluster", "namespace": "namespace", "espacio_de_nombres": "namespace", "carga": "workload", "contenedor": "container",
    "réplicas": "replicas", "replicas": "replicas", "cpu_solicitada": "cpu_request", "memoria_solicitada": "mem_request",
}


@dataclass
class ParseResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _num(value: str, name: str, lo: float, hi: float, line: int, errors: list[str]) -> float | None:
    v = (value or "").strip().replace(",", ".")
    if v == "":
        return None
    try:
        n = float(v)
    except ValueError:
        errors.append(f"Fila {line}: '{name}' no es numérico ({value!r})")
        return None
    if not lo <= n <= hi:
        errors.append(f"Fila {line}: '{name}' fuera de rango [{lo}, {hi}]")
        return None
    return n


def _tags(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"[;|]", raw or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()[:128]] = v.strip()[:256]
    return out


def _bool(raw: str) -> bool | None:
    return {"true": True, "1": True, "si": True, "sí": True, "yes": True, "false": False, "0": False, "no": False}.get((raw or "").strip().lower())


def _k8s_fields(row: dict[str, str], item: dict[str, Any], line: int, errs: list[str]) -> None:
    """Cargas de Kubernetes: CPU en núcleos (`0.5` o `500m`) y memoria con unidad (`512Mi`) o en bytes."""
    for col in ("namespace", "workload"):
        if not row.get(col, "").strip():
            errs.append(f"Fila {line}: k8s_workload requiere {col}")
    item.update(namespace=row.get("namespace", "").strip()[:63], workload=row.get("workload", "").strip()[:253],
                kind=row.get("kind", "").strip() or "Deployment", container=row.get("container", "").strip()[:253] or "app",
                cluster=row.get("cluster", "").strip()[:80] or "import", hpa=_bool(row.get("hpa", "")), oom_killed=_bool(row.get("oom_killed", "")))
    if item["kind"] not in ("Deployment", "StatefulSet", "DaemonSet"):
        errs.append(f"Fila {line}: kind debe ser Deployment, StatefulSet o DaemonSet")
    replicas = _num(row.get("replicas", ""), "replicas", 1, 10_000, line, errs)
    item["replicas"] = int(replicas) if replicas is not None else 1
    for col, parser in (("cpu_request", parse_cpu), ("cpu_limit", parse_cpu), ("cpu_p95_cores", parse_cpu), ("cpu_max_cores", parse_cpu),
                        ("cpu_avg_cores", parse_cpu), ("mem_request", parse_memory), ("mem_limit", parse_memory), ("mem_max", parse_memory)):
        raw = (row.get(col, "") or "").strip()
        if not raw:
            item[col] = None
            continue
        try:
            item[col] = parser(raw)
        except QuantityError:
            errs.append(f"Fila {line}: '{col}' no es una cantidad válida ({raw!r})")
            item[col] = None
    for col in ("cpu_request", "mem_request"):
        if not item.get(col):
            errs.append(f"Fila {line}: k8s_workload requiere {col}")


def parse_csv(text: str) -> ParseResult:
    res = ParseResult()
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        res.errors.append(f"Archivo demasiado grande (máx {MAX_BYTES // 1_000_000} MB)")
        return res
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [h.strip().lower().replace(" ", "_") for h in next(reader)]
    except StopIteration:
        res.errors.append("El archivo está vacío")
        return res
    header = [_ALIASES.get(h, h) for h in header]
    if "service" not in header or ("resource_id" not in header and "workload" not in header):
        res.errors.append("Faltan columnas obligatorias: service y resource_id (o workload para Kubernetes)")
        return res
    seen: set[str] = set()
    for line, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(res.rows) >= MAX_ROWS:
            res.errors.append(f"Se superó el máximo de {MAX_ROWS} filas")
            break
        row = {header[i]: cells[i] for i in range(min(len(header), len(cells)))}
        errs: list[str] = []
        rid = row.get("resource_id", "").strip()
        service = row.get("service", "").strip().lower()
        service = _SERVICE_ALIASES.get(service, service)
        if service not in SERVICES:
            errs.append(f"Fila {line}: service debe ser uno de {sorted(SERVICES)} (recibido {service!r})")
        if service == "k8s_workload" and not rid:
            ns, wl = row.get("namespace", "").strip(), row.get("workload", "").strip()
            rid = "/".join([row.get("cluster", "").strip() or "import", ns, row.get("kind", "").strip() or "Deployment", wl,
                            row.get("container", "").strip() or "app"]) if ns and wl else ""
        if not _ID.match(rid):
            errs.append(f"Fila {line}: resource_id inválido")
        elif rid in seen:
            errs.append(f"Fila {line}: resource_id duplicado ({rid})")
        item = {
            "resource_id": rid, "service": service, "name": row.get("name", "").strip()[:200] or None,
            "environment": row.get("environment", "").strip().lower()[:30],
            "region": row.get("region", "").strip()[:30] or "us-east-1",
            "instance_type": row.get("instance_type", "").strip()[:40] or None,
            "volume_type": row.get("volume_type", "").strip().lower()[:20] or None,
            "attached": _bool(row.get("attached", "")),
            "tags": _tags(row.get("tags", "")),
        }
        for col, lo, hi in (("size_gb", 0, 65536), ("monthly_cost", 0, 10_000_000), ("cpu_avg", 0, 100), ("cpu_max", 0, 100),
                            ("memory_avg", 0, 100), ("age_days", 0, 36500), ("observation_days", 0, 3650),
                            ("unattached_days", 0, 36500)):
            item[col] = _num(row.get(col, ""), col, lo, hi, line, errs)
        if service in COMPUTE and not item["instance_type"]:
            errs.append(f"Fila {line}: {service} requiere instance_type")
        if service in COMPUTE and service != "ec2" and item["monthly_cost"] is None:
            errs.append(f"Fila {line}: {service} requiere monthly_cost (no hay tabla de precios para ese proveedor)")
        if service in VOLUMES and item["size_gb"] is None:
            errs.append(f"Fila {line}: {service} requiere size_gb")
        if service == "rds":
            if not item["instance_type"]:
                errs.append(f"Fila {line}: rds requiere instance_type (clase, p. ej. db.m5.large)")
            item.update(engine=row.get("engine", "").strip().lower()[:30] or None, multi_az=_bool(row.get("multi_az", "")),
                        deletion_protection=_bool(row.get("deletion_protection", "")), read_replicas=_bool(row.get("read_replicas", "")),
                        connections_avg=_num(row.get("connections_avg", ""), "connections_avg", 0, 10_000_000, line, errs),
                        connections_max=_num(row.get("connections_max", ""), "connections_max", 0, 10_000_000, line, errs),
                        backup_retention_days=_num(row.get("backup_retention_days", ""), "backup_retention_days", 0, 35, line, errs))
        if service == "k8s_workload":
            _k8s_fields(row, item, line, errs)
        if errs:
            res.errors += errs
            continue
        seen.add(rid)
        res.rows.append(item)
    if not res.rows and not res.errors:
        res.errors.append("No hay filas de datos")
    return res


def _resource_from_row(r: dict[str, Any]) -> NormalizedResource:
    tags = dict(r.get("tags") or {})
    if r.get("name") and "Name" not in tags:
        tags["Name"] = r["name"]
    if r.get("environment") and not any(k.lower() in ("environment", "env") for k in tags):
        tags["Environment"] = r["environment"]
    svc = r["service"]
    provider, rtype = SERVICE_INFO.get(svc, ("aws", "storage"))
    obs = int(r["observation_days"] or 0) if r.get("observation_days") is not None else (30 if r.get("cpu_avg") is not None else 0)
    age = int(r["age_days"]) if r.get("age_days") is not None else None
    size = r.get("size_gb")
    cost = r.get("monthly_cost")
    common = dict(name=r.get("name"), environment=environment_from_tags(tags), cost_source="import", age_days=age, observation_days=obs, tags=tags)

    if svc == "k8s_workload":
        cpu_req, mem_req, replicas = r["cpu_request"], r["mem_request"], r.get("replicas") or 1
        if cost is None:
            cost = reserved_monthly_cost(cpu_req, mem_req, replicas, DEFAULT_CPU_CORE_HOUR, DEFAULT_MEM_GIB_HOUR)
        attrs = {"namespace": r["namespace"], "kind": r["kind"], "workload": r["workload"], "container": r["container"], "replicas": replicas,
                 "cpu_request": cpu_req, "mem_request": int(mem_req), "cpu_limit": r.get("cpu_limit"),
                 "mem_limit": int(r["mem_limit"]) if r.get("mem_limit") else None, "cpu_avg_cores": r.get("cpu_avg_cores"),
                 "cpu_p95_cores": r.get("cpu_p95_cores"), "cpu_max_cores": r.get("cpu_max_cores"),
                 "mem_avg_bytes": None, "mem_max_bytes": int(r["mem_max"]) if r.get("mem_max") else None,
                 "oom_killed": bool(r.get("oom_killed")), "hpa": bool(r.get("hpa")), "helm": {},
                 "cpu_hour_usd": DEFAULT_CPU_CORE_HOUR, "mem_gib_hour_usd": DEFAULT_MEM_GIB_HOUR, "cluster": r.get("cluster")}
        common.update(name=r.get("name") or f"{r['namespace']}/{r['workload']}/{r['container']}")
        return NormalizedResource(
            provider, rtype, "k8s_workload", r["resource_id"], r.get("cluster") or "import", state="running", monthly_cost=float(cost),
            cpu_avg=round(100 * r["cpu_avg_cores"] / cpu_req, 1) if r.get("cpu_avg_cores") is not None and cpu_req else None,
            cpu_max=round(100 * r["cpu_max_cores"] / cpu_req, 1) if r.get("cpu_max_cores") is not None and cpu_req else None,
            attributes=attrs, **common)

    region = r.get("region") or "us-east-1"
    if svc == "rds":
        if cost is None:
            cost = rds_monthly_cost(r.get("instance_type"), size, bool(r.get("multi_az")))
        attrs = {k: r.get(k) for k in ("engine", "multi_az", "connections_avg", "connections_max", "deletion_protection", "read_replicas",
                                       "backup_retention_days") if r.get(k) is not None}
        return NormalizedResource(provider, rtype, "rds", r["resource_id"], region, instance_type=r.get("instance_type"), state="available",
                                  monthly_cost=float(cost), cpu_avg=r.get("cpu_avg"), cpu_max=r.get("cpu_max"), size_gb=size, attributes=attrs, **common)
    if svc in COMPUTE:
        if cost is None:
            cost = instance_monthly_cost(r.get("instance_type")) or 0.0
        return NormalizedResource(provider, rtype, svc, r["resource_id"], region, instance_type=r.get("instance_type"), state="running",
                                  monthly_cost=float(cost), cpu_avg=r.get("cpu_avg"), cpu_max=r.get("cpu_max"), memory_avg=r.get("memory_avg"), **common)
    attached = r.get("attached")
    if svc in VOLUMES:
        if cost is None:
            cost = volume_monthly_cost(r.get("volume_type"), size)
        return NormalizedResource(provider, rtype, svc, r["resource_id"], region, volume_type=r.get("volume_type"),
                                  state="in-use" if attached else "available", monthly_cost=float(cost), attached=attached, size_gb=size,
                                  unattached_days=int(r["unattached_days"]) if r.get("unattached_days") is not None else None, **common)
    if cost is None:
        cost = snapshot_monthly_cost(size, provider)
    return NormalizedResource(provider, rtype, svc, r["resource_id"], region, monthly_cost=float(cost), size_gb=size, **common)


def rows_to_resources(rows: list[dict[str, Any]]) -> list[NormalizedResource]:
    return [_resource_from_row(r) for r in rows]


class ImportCollector:
    """Lee el último archivo importado de la cuenta 'import'. Lectura corta de BD, sin red."""

    def __init__(self, account: dict[str, Any]):
        self.org_id: UUID = account["organization_id"]
        self.account_id = account["id"]

    def collect(self) -> CollectionResult:
        from ..db import tenant_tx

        with tenant_tx(self.org_id) as conn:
            row = conn.execute("select filename, rows from imports where cloud_account_id = %s order by created_at desc limit 1",
                               (str(self.account_id),)).fetchone()
        if row is None:
            return CollectionResult(warnings=["No hay archivos importados"], partial=True)
        resources = rows_to_resources(row["rows"])
        today = date.today()
        costs = [CostRecord(r.resource_id, today - timedelta(days=d), round(r.monthly_cost / DAYS_PER_MONTH, 4), r.service,
                            r.region, "import") for r in resources for d in range(1, 8)]
        return CollectionResult(resources=resources, costs=costs,
                                warnings=[f"Datos importados de {row['filename']} (costos y uso según el archivo)"])
