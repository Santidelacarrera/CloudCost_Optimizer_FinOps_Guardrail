"""Conector GCP de SOLO LECTURA: instancias de Compute Engine, discos persistentes y snapshots + métricas de Cloud Monitoring.

Permisos (ver infrastructure/terraform/gcp-readonly-role): `roles/compute.viewer` y `roles/monitoring.viewer` sobre el proyecto.
Nunca se llama a una API de escritura. Los costos son una estimación por tabla de precios (los snapshots usan el tamaño
realmente almacenado, `storageBytes`, porque son incrementales).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import SNAPSHOT_GB_MONTH_BY_PROVIDER, instance_monthly_cost, volume_monthly_cost
from ..secrets import SecretResolver
from .base import CollectionResult
from .cloudhttp import GCP_HOSTS, BearerClient, JsonGetter, gcp_token_provider
from .common import WINDOW_DAYS, UnverifiedImages, age_days, finish, parse_ts

log = logging.getLogger(__name__)
COMPUTE = "https://compute.googleapis.com/compute/v1"
MONITORING = "https://monitoring.googleapis.com/v3"
MAX_PAGES = 200


def _last(url: str | None) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def _region_of(zone_or_region_url: str | None) -> str:
    name = _last(zone_or_region_url)
    parts = name.split("-")
    return "-".join(parts[:-1]) if len(parts) >= 3 else name           # us-central1-a -> us-central1 (una región ya lo es)


def _project_path(url: str | None) -> str:
    """`https://.../projects/p/zones/z/disks/d` -> `projects/p/zones/z/disks/d` (así los registra Terraform)."""
    text = url or ""
    return text[text.index("projects/"):] if "projects/" in text else text


class GcpCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, client: JsonGetter | None = None,
                 on_api_error=None, now: datetime | None = None):
        self.account = account
        self.secrets = secrets
        self.project = account["account_ref"]
        self.on_api_error = on_api_error or (lambda api: None)
        self._client = client
        self._now = now or datetime.now(timezone.utc)
        self.warnings: list[str] = []
        self.partial = False
        regions = [r.lower() for r in (account.get("regions") or [])]
        self.regions = None if (not regions or "all" in regions) else set(regions)

    def _http(self) -> JsonGetter:
        if self._client is None:
            cfg = self.account.get("provider_config") or {}
            secret = self.secrets.resolve(cfg.get("credentials_ref"))
            if not secret:
                raise PermissionError("Falta la clave de la cuenta de servicio de GCP (credentials_ref)")
            try:
                token = gcp_token_provider(secret)
            except ValueError as exc:
                raise PermissionError(str(exc)) from exc          # clave mal formada: no tiene sentido reintentar
            self._client = BearerClient(token, GCP_HOSTS)
        return self._client

    def _safe(self, api: str, fn, default):
        try:
            return fn()
        except Exception as exc:
            self.on_api_error(api)
            self.partial = True
            self.warnings.append(f"{api}: {getattr(exc, 'code', '') or type(exc).__name__}")
            log.warning("gcp api error api=%s err=%s", api, type(exc).__name__)
            return default

    def _paged(self, url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        bodies, params = [], dict(params or {})
        for _ in range(MAX_PAGES):
            body = self._http().get_json(url, params)
            bodies.append(body)
            nxt = body.get("nextPageToken")
            if not nxt:
                return bodies
            params["pageToken"] = nxt
        raise RuntimeError("demasiadas páginas")

    def _aggregated(self, kind: str, inner: str) -> list[dict[str, Any]]:
        rows = []
        for body in self._paged(f"{COMPUTE}/projects/{self.project}/aggregated/{kind}", {"maxResults": 500}):
            for scope in (body.get("items") or {}).values():
                rows += scope.get(inner, [])
        return rows

    def _in_scope(self, region: str) -> bool:
        return self.regions is None or region.lower() in self.regions

    def collect(self) -> CollectionResult:
        self._http()                  # credenciales ausentes o inválidas: error claro y sin reintentos, no «escaneo parcial»
        resources = self._instances() + self._disks() + self._snapshots()
        return finish(CollectionResult(), resources, self.warnings, self.partial)

    # ------------------------------------------------------------------ instancias
    def _instances(self) -> list[NormalizedResource]:
        rows = self._safe("compute:instances", lambda: self._aggregated("instances", "instances"), [])
        metrics = self._metrics() if any(r.get("status") == "RUNNING" for r in rows) else {}
        out = []
        for r in rows:
            region, zone = _region_of(r.get("zone")), _last(r.get("zone"))
            if not self._in_scope(region):
                continue
            labels = r.get("labels") or {}
            mtype = _last(r.get("machineType"))
            running = r.get("status") == "RUNNING"
            m = metrics.get(str(r.get("id")), {}) if running else {}
            age = age_days(parse_ts(r.get("creationTimestamp")), self._now)
            observed = min(WINDOW_DAYS, m.get("hours", 0) // 24, age if age is not None else WINDOW_DAYS)
            out.append(NormalizedResource(
                "gcp", "compute", "gce", f"projects/{self.project}/zones/{zone}/instances/{r['name']}", region,
                name=r["name"], environment=environment_from_tags(labels), instance_type=mtype,
                state="running" if running else "stopped", monthly_cost=instance_monthly_cost(mtype) or 0.0,
                cpu_avg=m.get("cpu_avg"), cpu_max=m.get("cpu_max"), memory_avg=m.get("memory_avg"),
                age_days=age, observation_days=int(observed), tags=dict(labels),
                attributes={"zone": zone, "instance_id": str(r.get("id"))}))
        return out

    def _series(self, metric_filter: str, aligner: str) -> dict[str, list[float]]:
        end = self._now
        params = {"filter": metric_filter,
                  "interval.startTime": (end - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "aggregation.alignmentPeriod": "3600s", "aggregation.perSeriesAligner": aligner, "view": "FULL"}
        values: dict[str, list[float]] = {}
        for body in self._paged(f"{MONITORING}/projects/{self.project}/timeSeries", params):
            for ts in body.get("timeSeries", []):
                iid = (ts.get("resource", {}).get("labels") or {}).get("instance_id")
                if iid is None:
                    continue
                values.setdefault(str(iid), []).extend(
                    float(p["value"]["doubleValue"]) for p in ts.get("points", []) if "doubleValue" in p.get("value", {}))
        return values

    def _metrics(self) -> dict[str, dict[str, Any]]:
        cpu_type = 'metric.type="compute.googleapis.com/instance/cpu/utilization"'
        mem_filter = 'metric.type="agent.googleapis.com/memory/percent_used" AND metric.labels.state="used"'
        avg = self._safe("monitoring:cpu", lambda: self._series(cpu_type, "ALIGN_MEAN"), {})
        peak = self._safe("monitoring:cpu_max", lambda: self._series(cpu_type, "ALIGN_MAX"), {})
        try:                                        # memoria solo existe con el Ops Agent instalado: su ausencia no es un fallo
            mem = self._series(mem_filter, "ALIGN_MEAN")
        except Exception:                           # noqa: BLE001
            mem = {}
        out: dict[str, dict[str, Any]] = {}
        for iid, vals in avg.items():
            if not vals:
                continue
            out[iid] = {"cpu_avg": round(100 * sum(vals) / len(vals), 2), "hours": len(vals),
                        "cpu_max": round(100 * max(peak.get(iid) or vals), 2)}
            if mem.get(iid):
                out[iid]["memory_avg"] = round(sum(mem[iid]) / len(mem[iid]), 2)
        return out

    # ------------------------------------------------------------------ discos
    def _disks(self) -> list[NormalizedResource]:
        out = []
        for d in self._safe("compute:disks", lambda: self._aggregated("disks", "disks"), []):
            zone_url = d.get("zone") or d.get("region")
            region = _region_of(zone_url)
            if not self._in_scope(region):
                continue
            labels = d.get("labels") or {}
            kind = "zones" if d.get("zone") else "regions"
            dtype = _last(d.get("type"))
            size = float(d.get("sizeGb") or 0)
            attached = bool(d.get("users"))
            created = parse_ts(d.get("creationTimestamp"))
            detached = parse_ts(d.get("lastDetachTimestamp")) or (created if not attached else None)
            attrs: dict[str, Any] = {"zone": _last(zone_url)}
            if "kubernetes.io/created-for" in (d.get("description") or ""):
                attrs["k8s_managed"] = True                    # PVC dinámico de GKE: no está en Terraform
            out.append(NormalizedResource(
                "gcp", "storage", "pd", f"projects/{self.project}/{kind}/{_last(zone_url)}/disks/{d['name']}", region,
                name=d["name"], environment=environment_from_tags(labels), volume_type=dtype,
                state="in-use" if attached else "available" if d.get("status") == "READY" else (d.get("status") or "").lower(),
                attached=attached, size_gb=size, monthly_cost=volume_monthly_cost(dtype, size) * (2 if kind == "regions" else 1),
                age_days=age_days(created, self._now), unattached_days=None if attached else age_days(detached, self._now),
                observation_days=WINDOW_DAYS, tags=dict(labels), attributes=attrs))
        return out

    # ------------------------------------------------------------------ snapshots
    def _snapshots(self) -> list[NormalizedResource]:
        used_by = self._image_sources()
        out = []
        for s in self._safe("compute:snapshots", lambda: self._paged_items(f"{COMPUTE}/projects/{self.project}/global/snapshots"), []):
            labels = s.get("labels") or {}
            path = f"projects/{self.project}/global/snapshots/{s['name']}"
            attrs: dict[str, Any] = {}
            if s.get("autoCreated"):
                attrs["managed_by"] = "snapshot-schedule"
            users = used_by.get(path.lower(), [])
            if users:
                attrs["ami_ids"] = users
            size = float(s.get("diskSizeGb") or 0)
            stored_gb = float(s["storageBytes"]) / 2**30 if s.get("storageBytes") else size
            out.append(NormalizedResource(
                "gcp", "storage", "pd_snapshot", path, "global", name=s["name"], environment=environment_from_tags(labels),
                state=(s.get("status") or "").lower(), size_gb=size,
                monthly_cost=round(SNAPSHOT_GB_MONTH_BY_PROVIDER["gcp"] * stored_gb, 2),
                cost_source="estimate" if s.get("storageBytes") else "estimate_upper_bound",
                age_days=age_days(parse_ts(s.get("creationTimestamp")), self._now), tags=dict(labels), attributes=attrs))
        return out                                  # los snapshots son globales: el filtro de región no aplica

    def _paged_items(self, url: str) -> list[dict[str, Any]]:
        rows = []
        for body in self._paged(url, {"maxResults": 500}):
            rows += body.get("items", [])
        return rows

    def _image_sources(self) -> dict[str, list[str]]:
        used: dict[str, list[str]] = {}

        def images() -> bool:
            for img in self._paged_items(f"{COMPUTE}/projects/{self.project}/global/images"):
                for src in (img.get("sourceSnapshot"), img.get("sourceDisk")):
                    if src:
                        used.setdefault(_project_path(src).lower(), []).append(img.get("name", "image"))
            return True

        if self._safe("compute:images", images, False) is not True:
            return UnverifiedImages()                     # sin saber qué imágenes dependen de los snapshots, no se propone borrar ninguno
        return used
