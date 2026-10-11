"""Conector Qdrant de SOLO LECTURA: tamaño, configuración de vectores y réplicas de cada colección, para estimar la RAM que ocupan.

Solo hace `GET /collections` y `GET /collections/{nombre}`. No crea, modifica ni borra nada. La clave de API (`api-key`) llega por referencia a un
secreto (`credentials_ref`) y no se registra. Un recurso = una colección.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from ..domain.datastores import (
    DEFAULT_DISK_GIB_MONTH,
    DEFAULT_RAM_GIB_MONTH,
    GIB,
    has_quantization,
    qdrant_ram_bytes,
    vector_specs,
)
from ..domain.models import NormalizedResource
from ..secrets import SecretResolver
from .base import CollectionResult
from .cloudhttp import CloudApiError, JsonGetter
from .common import finish
from .netguard import DestinationError, validate_service_url

log = logging.getLogger(__name__)
MAX_COLLECTIONS = 500
TIMEOUT = 20.0
MAX_RETRIES = 3
_NAME = re.compile(r"^[A-Za-z0-9._~-]{1,255}$")                    # nombres de colección válidos en Qdrant; se usan dentro de una URL


class QdrantClient:
    """GET con cabecera `api-key` hacia UN host. Reintenta 429 y 5xx con espera creciente; nada más."""

    def __init__(self, base_url: str, api_key: str | None, *, session: requests.Session | None = None, sleep=time.sleep):
        self._base, self._key, self._http, self._sleep = base_url, api_key, session or requests.Session(), sleep

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not url.startswith(self._base):
            raise CloudApiError(0, "host_not_allowed")
        headers = {"Accept": "application/json", **({"api-key": self._key} if self._key else {})}
        for attempt in range(MAX_RETRIES + 1):
            resp = self._http.get(url, params=params, headers=headers, timeout=TIMEOUT, allow_redirects=False)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                self._sleep(min(2.0 ** attempt, 20.0))
                continue
            if resp.status_code >= 400:
                raise CloudApiError(resp.status_code, "")
            return resp.json()
        raise CloudApiError(0, "retries_exhausted")


class QdrantCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, client: JsonGetter | None = None, on_api_error=None):
        self.account = account
        self.secrets = secrets
        cfg = account.get("provider_config") or {}
        self.cfg = cfg
        self.cluster = account["account_ref"]
        self.on_api_error = on_api_error or (lambda api: None)
        self._client = client
        self._base = cfg.get("qdrant_url") or "http://qdrant.invalid"
        self.warnings: list[str] = []
        self.partial = False
        self.ram_price = float(cfg.get("ram_gib_month_usd") or DEFAULT_RAM_GIB_MONTH)
        self.disk_price = float(cfg.get("disk_gib_month_usd") or DEFAULT_DISK_GIB_MONTH)

    def _http(self) -> JsonGetter:
        if self._client is None:
            try:
                self._base = validate_service_url(self.cfg.get("qdrant_url", ""), "URL de Qdrant")
            except DestinationError as exc:
                raise PermissionError(str(exc)) from exc               # configuración inválida: no tiene sentido reintentar
            key = self.secrets.resolve(self.cfg.get("credentials_ref"), self.account.get("organization_id")) if self.cfg.get("credentials_ref") else None
            self._client = QdrantClient(self._base, key)
        return self._client

    def _get(self, path: str, api: str) -> dict[str, Any] | None:
        try:
            body = self._http().get_json(f"{self._base}{path}")
        except PermissionError:
            raise
        except Exception as exc:                                       # noqa: BLE001
            self.on_api_error(api)
            self.partial = True
            self.warnings.append(f"{api}: {getattr(exc, 'status', None) or type(exc).__name__}")
            log.warning("qdrant api error api=%s err=%s", api, type(exc).__name__)
            return None
        return body.get("result") if isinstance(body, dict) else None

    def collect(self) -> CollectionResult:
        self._http()                                                   # URL o credenciales inválidas: error claro y sin reintentos
        listing = self._get("/collections", "qdrant:list_collections")
        names = [c["name"] for c in (listing or {}).get("collections", []) if isinstance(c, dict) and _NAME.match(str(c.get("name", "")))]
        if len(names) > MAX_COLLECTIONS:
            self.warnings.append(f"Se leyeron solo las primeras {MAX_COLLECTIONS} colecciones de {len(names)}.")
            self.partial = True
            names = names[:MAX_COLLECTIONS]
        resources: list[NormalizedResource] = []
        for name in names:
            info = self._get(f"/collections/{name}", "qdrant:get_collection")
            if info is None:
                continue
            resources.append(self._resource(name, info))
        if listing is not None and not names:
            self.warnings.append("Qdrant no tiene colecciones.")
        return finish(CollectionResult(), resources, self.warnings, self.partial)

    def _resource(self, name: str, info: dict[str, Any]) -> NormalizedResource:
        params = (info.get("config") or {}).get("params") or {}
        specs = vector_specs(params)
        quant = (info.get("config") or {}).get("quantization_config")
        points = int(info.get("points_count") or 0)
        indexed, vectors = info.get("indexed_vectors_count"), info.get("vectors_count")
        ratio = round(min(1.0, indexed / vectors), 3) if isinstance(indexed, int) and isinstance(vectors, int) and vectors > 0 else None
        replicas = int(params.get("replication_factor") or 1)
        ram = qdrant_ram_bytes(points, specs)
        cost = round(ram / GIB * self.ram_price * replicas, 2)
        attrs = {"collection": name, "points": points, "vectors": specs, "quantization": has_quantization(quant), "ram_bytes": ram,
                 "replication_factor": replicas, "shard_number": int(params.get("shard_number") or 1), "segments": info.get("segments_count"),
                 "indexed_ratio": ratio, "status": info.get("status"), "optimizer_status": info.get("optimizer_status") if isinstance(info.get("optimizer_status"), str) else "error",
                 "on_disk_payload": bool(params.get("on_disk_payload")), "ram_gib_month_usd": self.ram_price, "disk_gib_month_usd": self.disk_price,
                 "cluster": self.cluster}
        return NormalizedResource(
            "qdrant", "database", "qdrant_collection", f"{self.cluster}/{name}", "all", name=name,
            environment=str(self.cfg.get("environment") or "unknown"), state="running", monthly_cost=cost, cost_source="estimate",
            size_gb=round(ram / GIB, 4), observation_days=0, attributes=attrs)
