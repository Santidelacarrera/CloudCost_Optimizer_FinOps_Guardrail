"""Inicio de sesión único: une el protocolo OIDC (`auth/oidc.py`) con las cuentas de la base de datos.

Reglas de cuentas (cada una tiene su prueba):
  * La identidad es (emisor, sub). Un correo que ya pertenece a otra cuenta NUNCA se reasigna en silencio.
  * Una cuenta local con el mismo correo solo pasa a SSO si el administrador lo permitió (SSO_LINK_EXISTING), el IdP afirma que
    verificó el correo y la cuenta es de la misma organización. Al enlazarla pierde su contraseña local y se cierran sus sesiones.
  * El rol se recalcula en cada acceso a partir de los grupos/roles del IdP (el IdP manda; un cambio manual se pisa en el siguiente login).
  * Una cuenta deshabilitada en CloudCost no entra aunque el IdP la acepte.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from ..auth import oidc
from ..config import Settings
from . import accounts, audit

log = logging.getLogger(__name__)

SSO_PASSWORD_MARKER = "!sso"      # noqa: S105 — no es un hash válido: ninguna contraseña lo satisface
CALLBACK_PATH = "/api/auth/sso/callback"


class _NoRedirectSession:
    """requests sin seguir redirecciones: ningún endpoint del IdP debería redirigir, y seguirlas saltaría la comprobación de host."""

    def __init__(self) -> None:
        self._s = requests.Session()

    def get(self, url: str, **kw: Any):
        return self._s.get(url, allow_redirects=False, **kw)

    def post(self, url: str, **kw: Any):
        return self._s.post(url, allow_redirects=False, **kw)


def config_from_settings(settings: Settings) -> oidc.SsoConfig:
    provider = settings.sso_provider
    return oidc.SsoConfig(
        provider=provider, issuer=settings.sso_issuer, client_id=settings.sso_client_id,
        client_secret=settings.sso_client_secret.get_secret_value(),
        redirect_uri=settings.public_web_url.rstrip("/") + CALLBACK_PATH, scopes=settings.sso_scopes,
        tenant_id=settings.sso_tenant_id or None,
        allowed_domains=tuple(d.lower().lstrip("@") for d in oidc.split_csv(settings.sso_allowed_domains)),
        role_claim=settings.sso_role_claim, role_map=oidc.parse_role_map(settings.sso_role_map),
        default_role=settings.sso_default_role.upper() or None, jit=settings.sso_jit, link_existing=settings.sso_link_existing,
        required_amr=tuple(oidc.split_csv(settings.sso_required_amr)),
        label=settings.sso_label or oidc.DEFAULT_LABEL[provider])


def public_info(settings: Settings) -> dict:
    if not settings.sso_enabled:
        return {"sso_enabled": False, "sso_label": None}
    return {"sso_enabled": True, "sso_label": settings.sso_label or oidc.DEFAULT_LABEL[settings.sso_provider]}


def _to_auth_error(exc: oidc.SsoError) -> accounts.AuthError:
    return accounts.AuthError(exc.status, exc.message, exc.code)


def start(settings: Settings, http=None) -> dict:
    """Paso 1: devuelve la URL del IdP y el token de flujo que el BFF guarda en una cookie."""
    if not settings.sso_enabled:
        raise accounts.AuthError(404, "El inicio de sesión único no está activado.", "sso_disabled")
    cfg = config_from_settings(settings)
    try:
        meta = oidc.discover(cfg, http or _NoRedirectSession())
        url, flow = oidc.start_flow(cfg, meta, accounts._pepper(settings))
    except oidc.SsoError as exc:
        raise _to_auth_error(exc) from exc
    return {"authorize_url": url, "flow_token": flow}


def callback(settings: Settings, *, code: str, state: str, flow_token: str | None, ip: str | None, ua: str | None, http=None) -> dict:
    """Paso 2: canjea el código, valida el ID token y abre una sesión de CloudCost."""
    if not settings.sso_enabled:
        raise accounts.AuthError(404, "El inicio de sesión único no está activado.", "sso_disabled")
    cfg = config_from_settings(settings)
    http = http or _NoRedirectSession()
    try:
        flow = oidc.open_flow(flow_token, state, accounts._pepper(settings))
    except oidc.SsoError as exc:                       # sin flujo válido no se audita: cualquiera podría inundar el registro
        raise _to_auth_error(exc) from exc
    try:
        meta = oidc.discover(cfg, http)
        id_token = oidc.exchange_code(cfg, meta, code, flow.verifier, http)
        claims = oidc.validate_id_token(cfg, meta, id_token, flow.nonce, http)
        identity = oidc.identity_from_claims(cfg, claims)
        return _open_session(settings, cfg, identity, claims, ip, ua)
    except oidc.SsoError as exc:
        _audit_failure(settings, exc.code, ip)
        raise _to_auth_error(exc) from exc
    except accounts.AuthError as exc:
        _audit_failure(settings, exc.code, ip)
        raise


def _audit_failure(settings: Settings, code: str, ip: str | None) -> None:
    try:
        with accounts.auth_tx(settings.sso_org_id) as conn:
            audit.record(conn, settings.sso_org_id, audit.SSO_LOGIN_FAILED, entity_type="sso", payload={"code": code, "ip": ip})
    except Exception:                                  # noqa: BLE001 — auditar no debe tapar el error original
        log.warning("no se pudo auditar el fallo de SSO (%s)", code, exc_info=True)


def _open_session(settings: Settings, cfg: oidc.SsoConfig, ident: oidc.Identity, claims: dict, ip: str | None, ua: str | None) -> dict:
    org_id = settings.sso_org_id
    with accounts.auth_tx(org_id) as conn:
        if not conn.execute("select 1 from organizations where id = %s", (org_id,)).fetchone():
            raise accounts.AuthError(503, "La organización configurada para SSO no existe.", "sso_misconfigured")
        acc = conn.execute(
            "select id, organization_id, disabled_at, role from accounts where sso_issuer = %s and sso_subject = %s for update",
            (cfg.issuer, ident.subject)).fetchone()
        via = "sso"
        if acc is None:
            acc, via = _provision_or_link(conn, cfg, ident, org_id)
        else:
            if str(acc["organization_id"]) != str(org_id):
                raise accounts.AuthError(403, "Esta cuenta pertenece a otra organización.", "wrong_org")
            if acc["disabled_at"] is not None:
                raise accounts.AuthError(403, "Tu cuenta está deshabilitada. Contacta a un administrador.", "account_disabled")
        if acc["role"] != ident.role:
            conn.execute("update accounts set role = %s where id = %s", (ident.role, str(acc["id"])))
            audit.record(conn, org_id, audit.MEMBER_UPDATED, actor=accounts._actor(acc["id"]), entity_type="account", entity_id=acc["id"],
                         payload={"via": "sso", "role": ident.role, "previous_role": acc["role"]})
        token = accounts._new_session(conn, settings, acc["id"], org_id, "active", ip, ua)
        conn.execute("update accounts set last_login_at = now() where id = %s", (str(acc["id"]),))
        audit.record(conn, org_id, audit.LOGIN_SUCCEEDED, actor=accounts._actor(acc["id"]), entity_type="account", entity_id=acc["id"],
                     payload={"sso": cfg.provider, "via": via, "ip": ip, "amr": [str(a) for a in (claims.get("amr") or [])][:5]})
    return {"status": "ok", "token": token, "expires_in": settings.auth_session_hours * 3600}


def _provision_or_link(conn, cfg: oidc.SsoConfig, ident: oidc.Identity, org_id: str) -> tuple[dict, str]:
    existing = conn.execute("select id, organization_id, disabled_at, role, sso_subject from accounts where email = %s for update",
                            (ident.email,)).fetchone()
    if existing:
        if existing["sso_subject"] is not None:
            raise accounts.AuthError(409, "Ya existe una cuenta con este correo asociada a otra identidad. Contacta a un administrador.", "account_exists")
        if not (cfg.link_existing and ident.email_trusted and str(existing["organization_id"]) == str(org_id)):
            raise accounts.AuthError(409, "Ya existe una cuenta con este correo. Pide a un administrador que la vincule al inicio de sesión único.",
                                     "account_exists")
        if existing["disabled_at"] is not None:
            raise accounts.AuthError(403, "Tu cuenta está deshabilitada. Contacta a un administrador.", "account_disabled")
        conn.execute(
            """update accounts set sso_issuer = %s, sso_subject = %s, password_hash = %s, mfa_secret_enc = null, mfa_enabled_at = null,
                      email_verified_at = coalesce(email_verified_at, now()), password_changed_at = now() where id = %s""",
            (cfg.issuer, ident.subject, SSO_PASSWORD_MARKER, str(existing["id"])))
        conn.execute("delete from auth_recovery_codes where account_id = %s", (str(existing["id"]),))
        accounts._revoke_sessions(conn, existing["id"], "sso_linked")
        audit.record(conn, org_id, audit.ACCOUNT_LINKED, actor=accounts._actor(existing["id"]), entity_type="account", entity_id=existing["id"],
                     payload={"sso": cfg.provider})
        return existing, "linked"
    if not cfg.jit:
        raise accounts.AuthError(403, "Tu cuenta aún no fue dada de alta en CloudCost. Pide acceso a un administrador.", "not_provisioned")
    row = conn.execute(
        """insert into accounts (organization_id, email, full_name, role, password_hash, email_verified_at, sso_issuer, sso_subject)
           values (%s, %s, %s, %s, %s, case when %s then now() end, %s, %s) on conflict (email) do nothing
           returning id, organization_id, disabled_at, role""",
        (org_id, ident.email, ident.name, ident.role, SSO_PASSWORD_MARKER, ident.email_trusted, cfg.issuer, ident.subject)).fetchone()
    if not row:
        raise accounts.AuthError(409, "No pudimos crear tu cuenta. Inténtalo de nuevo.", "retry")
    audit.record(conn, org_id, audit.ACCOUNT_CREATED, actor=accounts._actor(row["id"]), entity_type="account", entity_id=row["id"],
                 payload={"via": "sso_jit", "sso": cfg.provider, "role": ident.role})
    return row, "jit"
