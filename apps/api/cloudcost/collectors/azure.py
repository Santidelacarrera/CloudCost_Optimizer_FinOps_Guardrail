"""Conector Azure de SOLO LECTURA: máquinas virtuales, discos administrados y snapshots + métricas de Azure Monitor.

Permisos (ver infrastructure/terraform/azure-readonly-role): roles integrados «Reader» y «Monitoring Reader» sobre la
suscripción. Nunca se llama a una API de escritura. Los costos son una estimación por tabla de precios.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.models import NormalizedResource, environment_from_tags
from ..domain.pricing import instance_monthly_cost, instance_spec, snapshot_monthly_cost, volume_monthly_cost
from ..secrets import SecretResolver
from .base import CollectionResult
from .cloudhttp import ARM_HOST, BearerClient, JsonGetter, azure_token_provider
from .common import WINDOW_DAYS, UnverifiedImages, age_days, finish, metric_summary, parse_ts

log = logging.getLogger(__name__)
ARM = f"https://{ARM_HOST}"
MAX_METRIC_VMS = 400                       # cada VM son 2 llamadas a Azure Monitor; el resto queda sin métricas (aviso)
MAX_PAGES = 200


def _rg(resource_id: str) -> str:
    parts = resource_id.lower().split("/")
    return parts[parts.index("resourcegroups") + 1] if "resourcegroups" in parts and parts.index("resourcegroups") + 1 < len(parts) else ""


def _prop(props: dict[str, Any], name: str) -> Any:
    """Azure no es consistente con mayúsculas en algunas propiedades (LastOwnershipUpdateTime)."""
    for k, v in props.items():
        if k.lower() == name.lower():
            return v
    return None


class AzureCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, client: JsonGetter | None = None,
                 on_api_error=None, now: datetime | None = None):
        self.account = account
        self.secrets = secrets
        self.subscription = account["account_ref"]
        self.on_api_error = on_api_error or (lambda api: None)
        self._client = client
        self._now = now or datetime.now(timezone.utc)
        self.warnings: list[str] = []
        self.partial = False
        regions = [r.lower() for r in (account.get("regions") or [])]
        self.regions = None if (not regions or "all" in regions) else set(regions)

    # ------------------------------------------------------------------ infraestructura
    def _http(self) -> JsonGetter:
        if self._client is None:
            cfg = self.account.get("provider_config") or {}
            secret = self.secrets.resolve(cfg.get("credentials_ref"))
            if not secret:
                raise PermissionError("Falta el secreto del service principal de Azure (credentials_ref)")
            try:
                token = azure_token_provider(cfg.get("tenant_id", ""), cfg.get("client_id", ""), secret)
            except ValueError as exc:
                raise PermissionError(str(exc)) from exc          # configuración inválida: no tiene sentido reintentar
            self._client = BearerClient(token, {ARM_HOST})
        return self._client

    def _safe(self, api: str, fn, default):
        try:
            return fn()
        except Exception as exc:                  # un fallo parcial no debe abortar el escaneo
            self.on_api_error(api)
            self.partial = True
            code = getattr(exc, "code", "") or type(exc).__name__
            self.warnings.append(f"{api}: {code}")
            log.warning("azure api error api=%s err=%s", api, type(exc).__name__)
            return default

    def _list(self, path: str, api_version: str, extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
        url, params = f"{ARM}/subscriptions/{self.subscription}/providers/{path}", {"api-version": api_version, **(extra or {})}
        rows: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            body = self._http().get_json(url, params)
            rows += body.get("value", [])
            url, params = body.get("nextLink"), None          # el cliente valida que nextLink siga en management.azure.com
            if not url:
                return rows
        raise RuntimeError("demasiadas páginas")

    def _in_scope(self, location: str | None) -> bool:
        return self.regions is None or (location or "").lower() in self.regions

    # ------------------------------------------------------------------ colección
    def collect(self) -> CollectionResult:
        self._http()                  # credenciales ausentes o inválidas: error claro y sin reintentos, no «escaneo parcial»
        resources = self._vms() + self._disks() + self._snapshots()
        return finish(CollectionResult(), resources, self.warnings, self.partial)

    # ------------------------------------------------------------------ VMs
    def _vms(self) -> list[NormalizedResource]:
        vms = self._safe("compute:virtualMachines", lambda: self._list("Microsoft.Compute/virtualMachines", "2024-03-01"), [])
        states = {}
        for row in self._safe("compute:virtualMachines:status",
                              lambda: self._list("Microsoft.Compute/virtualMachines", "2024-03-01", {"statusOnly": "true"}), []):
            codes = [s.get("code", "") for s in (row.get("properties", {}).get("instanceView", {}).get("statuses") or [])]
            power = next((c.split("/", 1)[1] for c in codes if c.startswith("PowerState/")), None)
            states[row["id"].lower()] = {"running": "running", "stopped": "stopped", "deallocated": "stopped"}.get(power or "", power)
        out: list[NormalizedResource] = []
        measured = 0
        for vm in vms:
            if not self._in_scope(vm.get("location")):
                continue
            props, tags = vm.get("properties", {}), vm.get("tags") or {}
            size = (props.get("hardwareProfile") or {}).get("vmSize")
            state = states.get(vm["id"].lower())
            created = parse_ts(props.get("timeCreated"))
            m: dict[str, Any] = {}
            if state == "running":
                if measured < MAX_METRIC_VMS:
                    measured += 1
                    m = self._vm_metrics(vm["id"], size)
                elif measured == MAX_METRIC_VMS:
                    measured += 1
                    self.warnings.append(f"Se midieron {MAX_METRIC_VMS} VMs; el resto quedó sin métricas")
                    self.partial = True
            age = age_days(created, self._now)
            observed = min(WINDOW_DAYS, m.get("hours", 0) // 24, age if age is not None else WINDOW_DAYS)
            out.append(NormalizedResource(
                "azure", "compute", "vm", vm["id"], vm.get("location", ""), name=vm.get("name"),
                environment=environment_from_tags(tags), instance_type=size, state=state,
                monthly_cost=instance_monthly_cost(size) or 0.0, cpu_avg=m.get("cpu_avg"), cpu_max=m.get("cpu_max"),
                memory_avg=m.get("memory_avg"), age_days=age, observation_days=int(observed), tags=dict(tags),
                attributes={"resource_group": _rg(vm["id"]), "os": (props.get("storageProfile", {}).get("osDisk") or {}).get("osType")}))
        return out

    def _vm_metrics(self, vm_id: str, size: str | None) -> dict[str, Any]:
        end = self._now
        timespan = f"{(end - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        base = {"api-version": "2018-01-01", "timespan": timespan, "interval": "PT1H", "aggregation": "Average,Maximum"}
        url = f"{ARM}{vm_id}/providers/Microsoft.Insights/metrics"

        def series(metric: str) -> tuple[list[float], list[float]]:
            body = self._http().get_json(url, {**base, "metricnames": metric})
            avg, mx = [], []
            for v in body.get("value", []):
                for ts in v.get("timeseries", []):
                    for pt in ts.get("data", []):
                        if pt.get("average") is not None:
                            avg.append(float(pt["average"]))
                        if pt.get("maximum") is not None:
                            mx.append(float(pt["maximum"]))
            return avg, mx

        out: dict[str, Any] = {}
        cpu = metric_summary(*self._safe("monitor:metrics", lambda: series("Percentage CPU"), ([], [])))
        if "avg" in cpu:
            out.update(cpu_avg=cpu["avg"], cpu_max=cpu.get("max"), hours=cpu["hours_with_data"])
        spec = instance_spec(size)
        if spec and "cpu_avg" in out:
            try:                                          # métrica de memoria de la plataforma: no existe en todas las VMs
                avail, _ = series("Available Memory Bytes")
            except Exception:                             # noqa: BLE001 — sin la métrica no se propone reducir (memoria desconocida)
                avail = []
            if avail:
                used = 100.0 - 100.0 * (sum(avail) / len(avail)) / (spec.memory_gib * 2**30)
                out["memory_avg"] = round(min(100.0, max(0.0, used)), 2)
        return out

    # ------------------------------------------------------------------ discos
    def _disks(self) -> list[NormalizedResource]:
        out = []
        for d in self._safe("compute:disks", lambda: self._list("Microsoft.Compute/disks", "2023-10-02"), []):
            if not self._in_scope(d.get("location")):
                continue
            props, tags = d.get("properties", {}), d.get("tags") or {}
            disk_state = props.get("diskState")
            attached = bool(d.get("managedBy") or d.get("managedByExtended")) or disk_state in ("Attached", "Reserved")
            state = "available" if (disk_state == "Unattached" and not attached) else "in-use" if attached else (disk_state or "").lower()
            sku = (d.get("sku") or {}).get("name")
            size = float(props.get("diskSizeGB") or 0)
            last_change = parse_ts(_prop(props, "LastOwnershipUpdateTime"))
            attrs: dict[str, Any] = {"resource_group": _rg(d["id"])}
            if tags.get("kubernetes.io-created-for-pv-name"):
                attrs["k8s_pv"] = tags["kubernetes.io-created-for-pv-name"]       # lo creó Kubernetes: no está en Terraform
            out.append(NormalizedResource(
                "azure", "storage", "disk", d["id"], d.get("location", ""), name=d.get("name"),
                environment=environment_from_tags(tags), volume_type=sku, state=state, attached=attached, size_gb=size,
                monthly_cost=volume_monthly_cost(sku, size), age_days=age_days(parse_ts(props.get("timeCreated")), self._now),
                unattached_days=age_days(last_change, self._now) if state == "available" else None,
                observation_days=WINDOW_DAYS, tags=dict(tags),
                attributes=attrs))
        return out

    # ------------------------------------------------------------------ snapshots
    def _snapshots(self) -> list[NormalizedResource]:
        used_by = self._image_sources()
        out = []
        for s in self._safe("compute:snapshots", lambda: self._list("Microsoft.Compute/snapshots", "2023-10-02"), []):
            if not self._in_scope(s.get("location")):
                continue
            props, tags = s.get("properties", {}), s.get("tags") or {}
            name, rg = s.get("name", ""), _rg(s["id"])
            attrs: dict[str, Any] = {"resource_group": rg, "incremental": props.get("incremental")}
            if name.startswith("AzureBackup_") or rg.startswith("azurebackuprg"):
                attrs["managed_by"] = "azure-backup"
            users = used_by.get(s["id"].lower(), [])
            if users:
                attrs["ami_ids"] = users
            size = float(props.get("diskSizeGB") or 0)
            out.append(NormalizedResource(
                "azure", "storage", "disk_snapshot", s["id"], s.get("location", ""), name=name,
                environment=environment_from_tags(tags), state=props.get("provisioningState"), size_gb=size,
                monthly_cost=snapshot_monthly_cost(size, "azure"), cost_source="estimate_upper_bound",
                age_days=age_days(parse_ts(props.get("timeCreated")), self._now), tags=dict(tags), attributes=attrs))
        return out

    def _image_sources(self) -> dict[str, list[str]]:
        """snapshot/disco -> imágenes o versiones de galería que lo usan. Si no se puede verificar, se bloquea la propuesta."""
        used: dict[str, list[str]] = {}

        def add(source_id: str | None, user: str) -> None:
            if source_id:
                used.setdefault(source_id.lower(), []).append(user)

        def images() -> bool:
            for img in self._list("Microsoft.Compute/images", "2023-09-01"):
                sp = img.get("properties", {}).get("storageProfile", {})
                for disk in [sp.get("osDisk") or {}] + list(sp.get("dataDisks") or []):
                    add((disk.get("snapshot") or {}).get("id"), img.get("name", "image"))
                    add((disk.get("managedDisk") or {}).get("id"), img.get("name", "image"))
            return True

        def galleries() -> bool:
            for g in self._list("Microsoft.Compute/galleries", "2023-07-03"):
                base = f"{ARM}{g['id']}"
                for im in self._http().get_json(f"{base}/images", {"api-version": "2023-07-03"}).get("value", []):
                    for ver in self._http().get_json(f"{ARM}{im['id']}/versions", {"api-version": "2023-07-03"}).get("value", []):
                        sp = ver.get("properties", {}).get("storageProfile", {})
                        label = f"{g.get('name')}/{im.get('name')}/{ver.get('name')}"
                        add((sp.get("source") or {}).get("id"), label)
                        add(((sp.get("osDiskImage") or {}).get("source") or {}).get("id"), label)
                        for dd in sp.get("dataDiskImages") or []:
                            add((dd.get("source") or {}).get("id"), label)
            return True

        ok = [self._safe("compute:images", images, False) is not False, self._safe("compute:galleries", galleries, False) is not False]
        if not all(ok):
            # No se pudo comprobar qué imágenes dependen de los snapshots: ninguno se propone para eliminar.
            return UnverifiedImages()
        return used
