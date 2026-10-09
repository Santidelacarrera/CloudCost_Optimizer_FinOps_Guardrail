"""Inicio de sesión único (SSO) con OpenID Connect: flujo de código de autorización + PKCE para Microsoft Entra ID y Okta.

Este módulo es lógica pura (sin base de datos ni FastAPI) para poder probarla entera con un IdP simulado:
  descubrimiento → URL de autorización (state, nonce, PKCE S256) → canje del código → validación del ID token → identidad y rol.

Decisiones de seguridad (cada una tiene su prueba):
  * `state`, `nonce` y el verificador PKCE viajan en un «token de flujo» firmado (HMAC, 10 min) que el navegador guarda en una cookie
    httpOnly: otra persona no puede inyectar un código propio en tu navegador (login CSRF) ni reutilizar una respuesta vieja.
  * Los metadatos de descubrimiento deben repetir el `issuer` configurado y apuntar al MISMO host (defensa contra mix-up).
  * ID token: solo RS256/ES256 (nunca `none` ni HS256), `aud` = nuestro client_id, `iss` exacto, `exp`/`iat`, `nonce`, y en Entra `tid`.
  * Las cuentas se enlazan por (issuer, sub), NO por correo: un IdP donde el correo es editable (Entra) permitiría quedarse con la
    cuenta de otra persona («nOAuth»). Enlazar una cuenta local existente exige confirmación explícita y correo verificado.
  * El rol sale de grupos/roles del IdP, con prioridad fija; sin coincidencia se aplica el rol por defecto o se rechaza.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

import jwt

# Prioridad de roles: si el usuario pertenece a varios grupos, gana el de más privilegio. Debe coincidir con security.ROLES.
ROLE_PRIORITY = ("ADMIN", "FINOPS", "SRE", "DEVELOPER", "AUDITOR", "VIEWER")
ALLOWED_ALGS = ("RS256", "ES256")
FLOW_TTL = 600
DISCOVERY_TTL = 3600
JWKS_TTL = 3600
JWKS_MIN_REFRESH = 60
HTTP_TIMEOUT = 10
_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
_TENANT = re.compile(r"^[0-9a-fA-F-]{36}$")
DEFAULT_LABEL = {"entra": "Microsoft Entra ID", "okta": "Okta", "generic": "SSO"}


class SsoError(Exception):
    """Fallo del inicio de sesión único. `code` es estable (la interfaz lo traduce); `message` es seguro de mostrar."""

    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class SsoConfig:
    provider: str
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = "openid email profile"
    tenant_id: str | None = None
    allowed_domains: tuple[str, ...] = ()
    role_claim: str = ""
    role_map: dict[str, str] = field(default_factory=dict)
    default_role: str | None = "VIEWER"
    jit: bool = True
    link_existing: bool = False
    required_amr: tuple[str, ...] = ()
    label: str = ""

    @property
    def effective_role_claim(self) -> str:
        return self.role_claim or ("roles" if self.provider == "entra" else "groups")


def split_csv(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def parse_role_map(text: str | None) -> dict[str, str]:
    """`grupo-o-rol:ROL,otro:ADMIN` -> {valor: ROL}. El valor puede contener ':' (se parte por el ÚLTIMO)."""
    out: dict[str, str] = {}
    for item in split_csv(text):
        value, sep, role = item.rpartition(":")
        role = role.strip().upper()
        if not sep or not value.strip() or role not in ROLE_PRIORITY:
            raise ValueError(f"SSO_ROLE_MAP inválido en {item!r}: usa «valor:ROL» con ROL en {', '.join(ROLE_PRIORITY)}")
        out[value.strip()] = role
    return out


def validate_issuer(provider: str, issuer: str, tenant_id: str | None) -> list[str]:
    problems = []
    parsed = urlparse(issuer or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        problems.append("SSO_ISSUER debe ser una URL https sin parámetros")
        return problems
    if provider == "entra":
        if not tenant_id or not _TENANT.match(tenant_id):
            problems.append("SSO_TENANT_ID (GUID del directorio) es obligatorio con SSO_PROVIDER=entra")
        elif tenant_id.lower() not in issuer.lower():
            problems.append("SSO_ISSUER de Entra debe contener tu tenant: https://login.microsoftonline.com/<TENANT>/v2.0 (no «common»)")
    return problems


# ------------------------------------------------------------------------------------------------ descubrimiento
@dataclass(frozen=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


_lock = threading.Lock()
_discovery: dict[str, tuple[float, Metadata]] = {}


def _same_https_host(url: str, issuer: str) -> bool:
    u, i = urlparse(url), urlparse(issuer)
    return u.scheme == "https" and u.hostname == i.hostname and not u.username


def discover(cfg: SsoConfig, http, *, now: Callable[[], float] = time.time) -> Metadata:
    with _lock:
        hit = _discovery.get(cfg.issuer)
        if hit and now() - hit[0] < DISCOVERY_TTL:
            return hit[1]
    try:
        resp = http.get(cfg.issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=HTTP_TIMEOUT)
        body = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:                                              # noqa: BLE001 — red caída, JSON inválido…
        raise SsoError("idp_unreachable", "No se pudo contactar con el proveedor de identidad.", 502) from exc
    if body.get("issuer") != cfg.issuer:                                  # Entra devuelve el mismo issuer que el configurado
        raise SsoError("idp_misconfigured", "El proveedor de identidad devolvió un emisor distinto del configurado.", 502)
    try:
        meta = Metadata(body["issuer"], body["authorization_endpoint"], body["token_endpoint"], body["jwks_uri"])
    except KeyError as exc:
        raise SsoError("idp_misconfigured", "Metadatos de OpenID incompletos.", 502) from exc
    if not all(_same_https_host(u, cfg.issuer) for u in (meta.authorization_endpoint, meta.token_endpoint, meta.jwks_uri)):
        raise SsoError("idp_misconfigured", "Los endpoints del proveedor no están en el host del emisor.", 502)
    with _lock:
        _discovery[cfg.issuer] = (now(), meta)
    return meta


def reset_caches() -> None:
    with _lock:
        _discovery.clear()
        _jwks.clear()


# ----------------------------------------------------------------------------------------------- token de flujo
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _flow_key(pepper: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"cloudcost/sso-flow/v1").derive(pepper)


@dataclass(frozen=True)
class FlowState:
    state: str
    nonce: str
    verifier: str


def start_flow(cfg: SsoConfig, meta: Metadata, pepper: bytes, *, now: Callable[[], float] = time.time) -> tuple[str, str]:
    """Devuelve (URL de autorización, token de flujo para la cookie del navegador)."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    payload = _b64(json.dumps({"s": state, "n": nonce, "v": verifier, "e": int(now()) + FLOW_TTL}, separators=(",", ":")).encode())
    token = payload + "." + _b64(hmac.new(_flow_key(pepper), payload.encode(), hashlib.sha256).digest())
    query = urlencode({"response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "scope": cfg.scopes,
                       "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
                       **({"response_mode": "query"} if cfg.provider == "entra" else {})})
    return f"{meta.authorization_endpoint}?{query}", token


def open_flow(token: str | None, state: str | None, pepper: bytes, *, now: Callable[[], float] = time.time) -> FlowState:
    try:
        payload, sig = (token or "").split(".")
        expected = _b64(hmac.new(_flow_key(pepper), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("firma")
        data = json.loads(_unb64(payload))
        if int(data["e"]) < now():
            raise SsoError("flow_expired", "El inicio de sesión tardó demasiado. Inténtalo de nuevo.")
        if not state or not hmac.compare_digest(str(state), str(data["s"])):
            raise ValueError("state")
        return FlowState(data["s"], data["n"], data["v"])
    except SsoError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise SsoError("invalid_flow", "La respuesta del proveedor no corresponde a un inicio de sesión iniciado en este navegador.") from exc


# ---------------------------------------------------------------------------------------------- canje y validación
def exchange_code(cfg: SsoConfig, meta: Metadata, code: str, verifier: str, http) -> str:
    try:
        resp = http.post(meta.token_endpoint, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri, "client_id": cfg.client_id,
            "client_secret": cfg.client_secret, "code_verifier": verifier}, timeout=HTTP_TIMEOUT)
        body = resp.json()
    except Exception as exc:                                              # noqa: BLE001
        raise SsoError("idp_unreachable", "No se pudo contactar con el proveedor de identidad.", 502) from exc
    if resp.status_code != 200 or not isinstance(body, dict) or not body.get("id_token"):
        # El detalle del IdP (error_description) puede contener datos del usuario: solo se conserva el código estándar.
        raise SsoError("token_exchange_failed", "El proveedor rechazó el inicio de sesión.", 401)
    return str(body["id_token"])


_jwks: dict[str, tuple[float, dict[str, Any]]] = {}


def _signing_key(meta: Metadata, kid: str | None, http, now: Callable[[], float]):
    def load() -> dict[str, Any]:
        try:
            resp = http.get(meta.jwks_uri, timeout=HTTP_TIMEOUT)
            keys = resp.json()["keys"] if resp.status_code == 200 else []
        except Exception as exc:                                          # noqa: BLE001
            raise SsoError("idp_unreachable", "No se pudo contactar con el proveedor de identidad.", 502) from exc
        out = {}
        for k in keys:
            try:
                out[k.get("kid", "")] = jwt.PyJWK(k)
            except jwt.PyJWTError:
                continue
        return out

    with _lock:
        hit = _jwks.get(meta.jwks_uri)
    fresh = hit is not None and now() - hit[0] < JWKS_TTL
    if not fresh or (kid not in hit[1] and now() - hit[0] > JWKS_MIN_REFRESH):      # clave nueva (rotación): se relee, con tope
        loaded = load()
        with _lock:
            _jwks[meta.jwks_uri] = (now(), loaded)
        hit = (now(), loaded)
    key = hit[1].get(kid or "")
    if key is None:
        raise SsoError("invalid_token", "El ID token está firmado con una clave desconocida.")
    return key.key


def validate_id_token(cfg: SsoConfig, meta: Metadata, id_token: str, nonce: str, http, *, now: Callable[[], float] = time.time) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") not in ALLOWED_ALGS:
            raise SsoError("invalid_token", "Algoritmo de firma no permitido.")
        key = _signing_key(meta, header.get("kid"), http, now)
        claims = jwt.decode(id_token, key, algorithms=[header["alg"]], audience=cfg.client_id, issuer=meta.issuer, leeway=60,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except SsoError:
        raise
    except jwt.PyJWTError as exc:
        raise SsoError("invalid_token", "El ID token no es válido o venció.") from exc
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SsoError("invalid_token", "El ID token no corresponde a este inicio de sesión.")
    aud = claims["aud"]
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != cfg.client_id:
        raise SsoError("invalid_token", "El ID token fue emitido para otras aplicaciones.")
    if cfg.provider == "entra" and str(claims.get("tid", "")).lower() != (cfg.tenant_id or "").lower():
        raise SsoError("wrong_tenant", "La cuenta no pertenece al directorio autorizado.", 403)
    if not claims.get("sub"):
        raise SsoError("invalid_token", "El ID token no trae identificador de usuario.")
    return claims


# ------------------------------------------------------------------------------------------------------- identidad
@dataclass(frozen=True)
class Identity:
    subject: str
    email: str
    name: str
    email_trusted: bool           # el IdP afirma que verificó el correo (email_verified / xms_edov)
    role: str


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1")


def identity_from_claims(cfg: SsoConfig, claims: dict[str, Any]) -> Identity:
    raw = claims.get("email") or (claims.get("preferred_username") if cfg.provider == "entra" else None) or claims.get("upn")
    email = str(raw or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise SsoError("no_email", "El proveedor no entregó un correo válido para tu cuenta.", 403)
    if cfg.allowed_domains and email.rsplit("@", 1)[1] not in cfg.allowed_domains:
        raise SsoError("domain_not_allowed", "Tu dominio de correo no está autorizado para entrar.", 403)
    if cfg.required_amr and not (set(map(str, claims.get("amr") or [])) & set(cfg.required_amr)):
        raise SsoError("mfa_required", "Tu proveedor de identidad debe verificar un segundo factor para entrar.", 403)
    name = str(claims.get("name") or email.split("@")[0]).strip()[:120] or email.split("@")[0]
    trusted = _truthy(claims.get("email_verified")) or _truthy(claims.get("xms_edov"))
    return Identity(str(claims["sub"]), email, name, trusted, role_from_claims(cfg, claims))


def role_from_claims(cfg: SsoConfig, claims: dict[str, Any]) -> str:
    claim = cfg.effective_role_claim
    values = claims.get(claim)
    values = [values] if isinstance(values, str) else [str(v) for v in values] if isinstance(values, list) else []
    matched = {cfg.role_map[v] for v in values if v in cfg.role_map}
    if matched:
        return next(r for r in ROLE_PRIORITY if r in matched)
    overage = claim == "groups" and "groups" in (claims.get("_claim_names") or {})
    if overage and not cfg.default_role:
        raise SsoError("groups_overage", "Perteneces a demasiados grupos para leerlos en el token; usa roles de aplicación.", 403)
    if cfg.default_role:
        return cfg.default_role
    raise SsoError("no_role", "Tu cuenta no tiene ningún grupo/rol autorizado en CloudCost.", 403)
