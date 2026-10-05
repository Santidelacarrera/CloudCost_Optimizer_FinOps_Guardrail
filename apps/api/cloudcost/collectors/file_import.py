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

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import DAYS_PER_MONTH, instance_monthly_cost, snapshot_monthly_cost, volume_monthly_cost
from .base import CollectionResult, CostRecord

MAX_ROWS = 5000
MAX_BYTES = 2_000_000
SERVICES = {"ec2", "ebs", "ebs_snapshot"}
_SERVICE_ALIASES = {"snapshot": "ebs_snapshot", "ebs-snapshot": "ebs_snapshot", "instancia": "ec2", "volumen": "ebs",
                    "volume": "ebs", "instance": "ec2"}
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

_ALIASES = {
    "id": "resource_id", "recurso": "resource_id", "id_recurso": "resource_id", "servicio": "service", "tipo": "service",
    "nombre": "name", "entorno": "environment", "ambiente": "environment", "region": "region", "región": "region",
    "tipo_instancia": "instance_type", "tipo_volumen": "volume_type", "tamano_gb": "size_gb", "tamaño_gb": "size_gb",
    "adjunto": "attached", "costo_mensual": "monthly_cost", "costo": "monthly_cost", "cpu_promedio": "cpu_avg",
    "cpu_maxima": "cpu_max", "cpu_máxima": "cpu_max", "memoria_promedio": "memory_avg", "edad_dias": "age_days",
    "edad_días": "age_days", "dias_observacion": "observation_days", "días_observación": "observation_days",
    "dias_sin_adjuntar": "unattached_days", "etiquetas": "tags",
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
    if "resource_id" not in header or "service" not in header:
        res.errors.append("Faltan columnas obligatorias: resource_id y service")
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
        if not _ID.match(rid):
            errs.append(f"Fila {line}: resource_id inválido")
        elif rid in seen:
            errs.append(f"Fila {line}: resource_id duplicado ({rid})")
        if service not in SERVICES:
            errs.append(f"Fila {line}: service debe ser uno de {sorted(SERVICES)} (recibido {service!r})")
        item = {
            "resource_id": rid, "service": service, "name": row.get("name", "").strip()[:200] or None,
            "environment": row.get("environment", "").strip().lower()[:30],
            "region": row.get("region", "").strip()[:30] or "us-east-1",
            "instance_type": row.get("instance_type", "").strip()[:40] or None,
            "volume_type": row.get("volume_type", "").strip().lower()[:20] or None,
            "attached": {"true": True, "1": True, "si": True, "sí": True, "yes": True, "false": False, "0": False,
                         "no": False}.get(row.get("attached", "").strip().lower()),
            "tags": _tags(row.get("tags", "")),
        }
        for col, lo, hi in (("size_gb", 0, 65536), ("monthly_cost", 0, 10_000_000), ("cpu_avg", 0, 100), ("cpu_max", 0, 100),
                            ("memory_avg", 0, 100), ("age_days", 0, 36500), ("observation_days", 0, 3650),
                            ("unattached_days", 0, 36500)):
            item[col] = _num(row.get(col, ""), col, lo, hi, line, errs)
        if service == "ec2" and not item["instance_type"]:
            errs.append(f"Fila {line}: ec2 requiere instance_type")
        if service == "ebs" and item["size_gb"] is None:
            errs.append(f"Fila {line}: ebs requiere size_gb")
        if errs:
            res.errors += errs
            continue
        seen.add(rid)
        res.rows.append(item)
    if not res.rows and not res.errors:
        res.errors.append("No hay filas de datos")
    return res


def rows_to_resources(rows: list[dict[str, Any]]) -> list[NormalizedResource]:
    out: list[NormalizedResource] = []
    for r in rows:
        tags = dict(r.get("tags") or {})
        if r.get("name") and "Name" not in tags:
            tags["Name"] = r["name"]
        if r.get("environment") and not any(k.lower() in ("environment", "env") for k in tags):
            tags["Environment"] = r["environment"]
        svc = r["service"]
        size = r.get("size_gb")
        cost = r.get("monthly_cost")
        if cost is None:
            cost = (instance_monthly_cost(r.get("instance_type")) or 0.0) if svc == "ec2" else \
                volume_monthly_cost(r.get("volume_type"), size) if svc == "ebs" else snapshot_monthly_cost(size)
        attached = r.get("attached")
        out.append(NormalizedResource(
            "aws", "compute" if svc == "ec2" else "storage", svc, r["resource_id"], r.get("region") or "us-east-1",
            name=r.get("name"), environment=environment_from_tags(tags), instance_type=r.get("instance_type"),
            volume_type=r.get("volume_type"),
            state=("running" if svc == "ec2" else ("in-use" if attached else "available") if svc == "ebs" else None),
            monthly_cost=float(cost), cost_source="import", cpu_avg=r.get("cpu_avg"), cpu_max=r.get("cpu_max"),
            memory_avg=r.get("memory_avg"), attached=attached if svc == "ebs" else None, size_gb=size,
            age_days=int(r["age_days"]) if r.get("age_days") is not None else None,
            observation_days=int(r["observation_days"] or 0) if r.get("observation_days") is not None else
            (30 if r.get("cpu_avg") is not None else 0),
            unattached_days=int(r["unattached_days"]) if r.get("unattached_days") is not None else None, tags=tags))
    return out


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
