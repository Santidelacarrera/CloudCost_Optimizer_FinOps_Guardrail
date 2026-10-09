"""Cliente HTTP mínimo y de SOLO LECTURA para las APIs REST de Azure y GCP, y obtención de tokens.

Por qué REST y no los SDK: los SDK de Azure/GCP son decenas de paquetes; aquí solo se hacen GET de inventario y métricas.
Garantías:
  * Solo GET (la única excepción es el POST del token OAuth2 a un endpoint fijo).
  * Solo HTTPS y solo hacia los hosts permitidos: un `nextLink` manipulado no puede sacar el token hacia otro servidor.
  * Reintentos acotados con 429/5xx respetando Retry-After.
Los tests inyectan un cliente falso con la misma interfaz (`get_json(url, params=None) -> dict`).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import requests

ARM_HOST = "management.azure.com"
LOGIN_HOST = "login.microsoftonline.com"
GCP_HOSTS = frozenset({"compute.googleapis.com", "monitoring.googleapis.com"})
GCP_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — URL pública, no es un secreto
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform.read-only"

_TENANT_RE = re.compile(r"^([0-9a-fA-F-]{36}|[A-Za-z0-9.-]{3,253})$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_RETRIES = 3
TIMEOUT = 30


class CloudApiError(RuntimeError):
    """Error de una API de nube. Nunca incluye cabeceras ni tokens, solo estado y código de error."""

    def __init__(self, status: int, code: str = ""):
        super().__init__(f"HTTP {status} {code}".strip())
        self.status, self.code = status, code


class JsonGetter(Protocol):
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


def is_guid(value: str | None) -> bool:
    return bool(value and _GUID_RE.match(value))


def is_tenant(value: str | None) -> bool:
    return bool(value and _TENANT_RE.match(value))


class BearerClient:
    """GET con token Bearer hacia un conjunto cerrado de hosts."""

    def __init__(self, token_provider: Callable[[], str], allowed_hosts: frozenset[str] | set[str], *,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep):
        self._token = token_provider
        self._hosts = frozenset(allowed_hosts)
        self._http = session or requests.Session()
        self._sleep = sleep

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self._hosts:
            raise CloudApiError(0, "host_not_allowed")
        for attempt in range(MAX_RETRIES + 1):
            resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token()}",
                                                               "Accept": "application/json"}, timeout=TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2.0 ** attempt
                self._sleep(min(max(delay, 0.5), 30.0))
                continue
            if resp.status_code >= 400:
                raise CloudApiError(resp.status_code, _error_code(resp))
            return resp.json()
        raise CloudApiError(0, "retries_exhausted")           # inalcanzable, por claridad


def _error_code(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or err.get("status") or "")[:80]
    return str(err or "")[:80]


class _CachedToken:
    def __init__(self, fetch: Callable[[], tuple[str, float]], clock: Callable[[], float] = time.time):
        self._fetch, self._clock = fetch, clock
        self._value: str | None = None
        self._expires = 0.0

    def __call__(self) -> str:
        if self._value is None or self._clock() >= self._expires - 120:
            self._value, ttl = self._fetch()
            self._expires = self._clock() + ttl
        return self._value


def azure_token_provider(tenant_id: str, client_id: str, client_secret: str, *, session: requests.Session | None = None,
                         clock: Callable[[], float] = time.time) -> Callable[[], str]:
    """Credenciales de cliente (service principal) con alcance de ARM. El secreto solo vive en memoria."""
    if not is_tenant(tenant_id) or not is_guid(client_id):
        raise ValueError("tenant_id o client_id inválidos")
    http = session or requests.Session()

    def fetch() -> tuple[str, float]:
        resp = http.post(f"https://{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token",
                         data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                               "scope": f"https://{ARM_HOST}/.default"}, timeout=TIMEOUT)
        if resp.status_code >= 400:
            raise CloudApiError(resp.status_code, _error_code(resp) or "token")
        body = resp.json()
        return body["access_token"], float(body.get("expires_in", 3000))

    return _CachedToken(fetch, clock)


def gcp_token_provider(service_account_json: str, *, session: requests.Session | None = None,
                       clock: Callable[[], float] = time.time) -> Callable[[], str]:
    """Cuenta de servicio de solo lectura: aserción JWT firmada con su clave (RS256) canjeada por un token de acceso."""
    import jwt  # PyJWT[crypto], ya dependencia de la API

    try:
        key = json.loads(service_account_json)
        email, private_key = key["client_email"], key["private_key"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("La clave de la cuenta de servicio no es un JSON válido") from exc
    http = session or requests.Session()

    def fetch() -> tuple[str, float]:
        now = int(clock())
        assertion = jwt.encode({"iss": email, "scope": GCP_SCOPE, "aud": GCP_TOKEN_URL, "iat": now, "exp": now + 3000},
                               private_key, algorithm="RS256")
        resp = http.post(GCP_TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                              "assertion": assertion}, timeout=TIMEOUT)
        if resp.status_code >= 400:
            raise CloudApiError(resp.status_code, _error_code(resp) or "token")
        body = resp.json()
        return body["access_token"], float(body.get("expires_in", 3000))

    return _CachedToken(fetch, clock)
