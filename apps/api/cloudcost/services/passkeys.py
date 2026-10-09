"""Llaves de acceso (WebAuthn): registro, segundo factor y entrada sin contraseña. La criptografía vive en `auth/webauthn.py`.

Reglas (cada una tiene su prueba):
  * Cada reto se emite una sola vez, vence a los 5 min y se CONSUME al verificar (haya éxito o no): una respuesta no se puede repetir.
  * Registrar o quitar una llave exige la contraseña (como el 2FA con app): una sesión robada no basta para añadir la llave del atacante.
  * Una llave siempre verifica al usuario (PIN/biometría): por eso entra sin contraseña y cuenta como segundo factor.
  * Los fallos de una llave no bloquean la cuenta (nadie puede adivinar una firma): solo cuentan contra la IP y, en el paso de 2FA,
    contra la sesión pendiente, igual que los códigos TOTP.
  * Las cuentas de SSO (sin contraseña local) no registran llaves: su acceso lo gobierna el proveedor de identidad.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from ..auth import crypto, mailer, webauthn
from ..config import Settings
from . import audit
from .accounts import (
    IP_FAIL_LIMIT,
    IP_WINDOW,
    MFA_FAIL_LIMIT,
    MFA_WINDOW,
    AuthError,
    Outbox,
    _actor,
    _finish_login,
    _load_account,
    _locked_error,
    _pepper,
    _reauth,
    _recent,
    _record,
    _revoke_one,
    _revoke_sessions,
    auth_tx,
)

log = logging.getLogger(__name__)

MAX_PASSKEYS = 10
OPTIONS_PER_IP, OPTIONS_WINDOW = 60, 900
SSO_MARKER = "!sso"
DEFAULT_NAME = "Llave de acceso"


# ---------------------------------------------------------------------------------------------------------- utilidades
def _rp(settings: Settings) -> tuple[str, str, tuple[str, ...]]:
    return settings.rp_id, settings.webauthn_rp_name, settings.webauthn_origin_list


def _fail(exc: webauthn.WebAuthnError) -> AuthError:
    return AuthError(exc.status, exc.message, exc.code)


def _need_enabled(settings: Settings) -> None:
    if not settings.passkeys_enabled:
        raise AuthError(404, "Las llaves de acceso están desactivadas.", "passkeys_disabled")


def _issue_challenge(conn, purpose: str, account_id: Any = None) -> bytes:
    conn.execute("delete from webauthn_challenges where expires_at < now()")                      # limpieza oportunista
    challenge = webauthn.new_challenge()
    conn.execute(
        "insert into webauthn_challenges (token_hash, purpose, account_id, expires_at) values (%s, %s, %s, now() + make_interval(secs => %s))",
        (crypto.hash_token(webauthn.b64u(challenge)), purpose, str(account_id) if account_id else None, webauthn.CHALLENGE_TTL))
    return challenge


def _consume_challenge(conn, challenge_b64: str, purpose: str) -> dict | None:
    return conn.execute(
        "delete from webauthn_challenges where token_hash = %s and purpose = %s and expires_at > now() returning account_id",
        (crypto.hash_token(challenge_b64), purpose)).fetchone()


def _parts(credential: dict, *, registration: bool) -> dict:
    """Extrae y decodifica los campos de la respuesta del navegador (todo viene en base64url)."""
    try:
        resp = credential["response"]
        out = {"id": credential["id"], "client_data": webauthn.unb64u(resp["clientDataJSON"])}
        if registration:
            out["attestation"] = webauthn.unb64u(resp["attestationObject"])
            out["transports"] = [t for t in (resp.get("transports") or []) if t in ("usb", "nfc", "ble", "internal", "hybrid", "smart-card")][:6]
        else:
            out["auth_data"] = webauthn.unb64u(resp["authenticatorData"])
            out["signature"] = webauthn.unb64u(resp["signature"])
            out["user_handle"] = webauthn.unb64u(resp["userHandle"]) if resp.get("userHandle") else None
        return out
    except (KeyError, TypeError, AttributeError) as exc:
        raise AuthError(400, "La respuesta de la llave de acceso no es válida.", "invalid_response") from exc
    except webauthn.WebAuthnError as exc:
        raise _fail(exc) from exc


def _name(raw: str | None) -> str:
    cleaned = " ".join((raw or "").split())[:60]
    return cleaned or DEFAULT_NAME


def _public(row: dict) -> dict:
    return {"id": str(row["id"]), "name": row["name"], "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            "synced": bool(row["backup_eligible"]), "transports": list(row["transports"] or [])}


# ---------------------------------------------------------------------------------------------------------- gestión (sesión activa)
def list_passkeys(p) -> list[dict]:
    with auth_tx(p.org_id) as conn:
        rows = conn.execute("select id, name, created_at, last_used_at, backup_eligible, transports from webauthn_credentials "
                            "where account_id = %s and organization_id = %s order by created_at", (p.account_id, str(p.org_id))).fetchall()
    return [_public(r) for r in rows]


def register_options(settings: Settings, p, password: str) -> dict:
    _need_enabled(settings)
    rp_id, rp_name, _ = _rp(settings)
    err: AuthError | None = None
    out: dict = {}
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        if acc["password_hash"] == SSO_MARKER:
            err = AuthError(409, "Tu acceso lo gestiona el inicio de sesión único; no necesitas llaves de acceso aquí.", "sso_account")
        else:
            existing = conn.execute("select credential_id from webauthn_credentials where account_id = %s", (str(acc["id"]),)).fetchall()
            if len(existing) >= MAX_PASSKEYS:
                err = AuthError(409, f"Ya tienes {MAX_PASSKEYS} llaves de acceso. Quita alguna para añadir otra.", "too_many_passkeys")
            else:
                err = _reauth(conn, settings, acc, password)
        if not err:
            challenge = _issue_challenge(conn, "register", acc["id"])
            out = webauthn.creation_options(rp_id=rp_id, rp_name=rp_name, user_id=uuid.UUID(str(acc["id"])).bytes, user_name=acc["email"],
                                            display_name=acc["full_name"], challenge=challenge, exclude=[bytes(r["credential_id"]) for r in existing])
    if err:
        raise err
    return out


def register_finish(settings: Settings, p, credential: dict, name: str | None, outbox: Outbox) -> dict:
    _need_enabled(settings)
    rp_id, _, origins = _rp(settings)
    parts = _parts(credential, registration=True)
    err: AuthError | None = None
    out: dict = {}
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        try:
            challenge_b64 = webauthn.client_challenge(parts["client_data"])
        except webauthn.WebAuthnError as exc:
            raise _fail(exc) from exc
        row = _consume_challenge(conn, challenge_b64, "register")
        if not row or str(row["account_id"]) != str(acc["id"]):
            err = AuthError(400, "La verificación venció o ya se usó. Inténtalo de nuevo.", "invalid_challenge")
        else:
            try:
                new = webauthn.verify_registration(client_data_json=parts["client_data"], attestation_object=parts["attestation"], credential_id_b64=parts["id"],
                                                   challenge=webauthn.unb64u(challenge_b64), origins=origins, rp_id=rp_id)
            except webauthn.WebAuthnError as exc:
                err = _fail(exc)
        if not err:
            first_factor = not conn.execute("select 1 from webauthn_credentials where account_id = %s", (str(acc["id"]),)).fetchone() \
                and acc["mfa_enabled_at"] is None
            inserted = conn.execute(
                """insert into webauthn_credentials (account_id, organization_id, credential_id, public_key, alg, sign_count, aaguid, transports,
                                                     backup_eligible, backup_state, name)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (credential_id) do nothing returning id""",
                (str(acc["id"]), str(p.org_id), new.credential_id, new.public_key, new.alg, new.sign_count, new.aaguid, parts["transports"],
                 new.backup_eligible, new.backup_state, _name(name))).fetchone()
            if not inserted:
                err = AuthError(409, "Esa llave de acceso ya está registrada.", "passkey_exists")
            else:
                if first_factor:                                                       # primer segundo factor: se cierran las demás sesiones
                    _revoke_sessions(conn, acc["id"], "passkey_added", except_id=p.session_id)
                audit.record(conn, p.org_id, audit.PASSKEY_ADDED, actor=_actor(acc["id"]), entity_type="account", entity_id=acc["id"],
                             payload={"synced": new.backup_eligible, "alg": new.alg})
                outbox.append(mailer.security_notice_mail(acc["email"], "Añadiste una llave de acceso a tu cuenta."))
                out = {"status": "added", "id": str(inserted["id"])}
    if err:
        raise err
    return out


def remove_passkey(settings: Settings, p, passkey_id: str, password: str, outbox: Outbox) -> None:
    err: AuthError | None = None
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        err = _reauth(conn, settings, acc, password)
        if not err:
            try:
                uuid.UUID(passkey_id)
            except ValueError:
                err = AuthError(404, "No encontramos esa llave de acceso.", "not_found")
        if not err:
            gone = conn.execute("delete from webauthn_credentials where id = %s and account_id = %s returning name",
                                (passkey_id, str(acc["id"]))).fetchone()
            if not gone:
                err = AuthError(404, "No encontramos esa llave de acceso.", "not_found")
            else:
                _revoke_sessions(conn, acc["id"], "passkey_removed", except_id=p.session_id)
                audit.record(conn, p.org_id, audit.PASSKEY_REMOVED, actor=_actor(acc["id"]), entity_type="account", entity_id=acc["id"])
                outbox.append(mailer.security_notice_mail(acc["email"], "Quitaste una llave de acceso de tu cuenta y cerramos tus otras sesiones."))
    if err:
        raise err


# ---------------------------------------------------------------------------------------------------------- verificación común
def _check_assertion(conn, settings: Settings, credential: dict, purpose: str, *, account_id: Any = None) -> tuple[dict | None, AuthError | None]:
    """Consume el reto, busca la llave y verifica la firma. Devuelve (fila de la llave con su cuenta, None) o (None, error).
    Si `account_id` se indica (paso de 2FA), la llave debe ser de esa cuenta."""
    rp_id, _, origins = _rp(settings)
    parts = _parts(credential, registration=False)
    try:
        challenge_b64 = webauthn.client_challenge(parts["client_data"])
        raw_id = webauthn.unb64u(parts["id"])
    except webauthn.WebAuthnError as exc:
        return None, _fail(exc)
    ch = _consume_challenge(conn, challenge_b64, purpose)
    if not ch or (account_id is not None and str(ch["account_id"]) != str(account_id)):
        return None, AuthError(400, "La verificación venció o ya se usó. Inténtalo de nuevo.", "invalid_challenge")
    row = conn.execute(
        """select c.id as passkey_id, c.public_key, c.alg, c.sign_count, a.id, a.organization_id, a.email, a.disabled_at, a.email_verified_at,
                  a.password_hash
             from webauthn_credentials c join accounts a on a.id = c.account_id where c.credential_id = %s""", (raw_id,)).fetchone()
    if not row or (account_id is not None and str(row["id"]) != str(account_id)):
        return None, AuthError(401, "Esa llave de acceso no está registrada en tu cuenta.", "unknown_passkey")
    try:
        result = webauthn.verify_assertion(client_data_json=parts["client_data"], authenticator_data=parts["auth_data"], signature=parts["signature"],
                                           user_handle=parts["user_handle"], challenge=webauthn.unb64u(challenge_b64), origins=origins, rp_id=rp_id,
                                           public_key=bytes(row["public_key"]), alg=row["alg"], stored_sign_count=int(row["sign_count"]))
    except webauthn.WebAuthnError as exc:
        return None, _fail(exc)
    if result.user_handle is not None and result.user_handle != uuid.UUID(str(row["id"])).bytes:
        return None, AuthError(401, "La llave de acceso no corresponde a esta cuenta.", "unknown_passkey")
    # Avance del contador con comprobación optimista: dos respuestas concurrentes con el mismo contador no pasan las dos.
    moved = conn.execute("update webauthn_credentials set sign_count = %s, backup_state = %s, last_used_at = now() where id = %s and sign_count = %s "
                         "returning id", (result.sign_count, result.backup_state, str(row["passkey_id"]), int(row["sign_count"]))).fetchone()
    if not moved:
        return None, AuthError(401, "La llave de acceso parece clonada. Por seguridad no se aceptó.", "counter_regression")
    return row, None


# ---------------------------------------------------------------------------------------------------------- segundo factor
def _pending(conn, pending_token: str) -> dict:
    s = conn.execute(
        """select s.id as session_id, a.id, a.organization_id, a.email, a.disabled_at, a.mfa_enabled_at,
                  (select count(*)::int from webauthn_credentials w where w.account_id = a.id) as passkeys
             from auth_sessions s join accounts a on a.id = s.account_id
            where s.token_hash = %s and s.kind = 'mfa_pending' and s.revoked_at is null and s.expires_at > now() for update of s""",
        (crypto.hash_token(pending_token),)).fetchone()
    if not s or s["disabled_at"] is not None:
        raise AuthError(401, "La verificación venció. Inicia sesión de nuevo.", "mfa_expired")
    conn.execute("select set_config('app.current_org', %s, true)", (str(s["organization_id"]),))
    return s


def mfa_options(settings: Settings, pending_token: str) -> dict:
    _need_enabled(settings)
    rp_id, _, _ = _rp(settings)
    with auth_tx() as conn:
        s = _pending(conn, pending_token)
        creds = conn.execute("select credential_id, transports from webauthn_credentials where account_id = %s", (str(s["id"]),)).fetchall()
        if not creds:
            raise AuthError(404, "Tu cuenta no tiene llaves de acceso.", "no_passkeys")
        challenge = _issue_challenge(conn, "mfa", s["id"])
        options = webauthn.request_options(rp_id=rp_id, challenge=challenge, allow=[(bytes(c["credential_id"]), list(c["transports"] or [])) for c in creds])
    return {"options": options, "totp": s["mfa_enabled_at"] is not None}


def mfa_finish(settings: Settings, *, pending_token: str, credential: dict, ip: str | None, ua: str | None) -> dict:
    _need_enabled(settings)
    err: AuthError | None = None
    out: dict = {}
    with auth_tx() as conn:
        s = _pending(conn, pending_token)
        key = crypto.hash_email(str(s["id"]), _pepper(settings))
        if _recent(conn, "mfa", email_hash=key, seconds=MFA_WINDOW, only_failures=True) >= MFA_FAIL_LIMIT:
            _revoke_one(conn, s["session_id"], "mfa_attempts")
            err = AuthError(429, "Demasiados intentos. Inicia sesión de nuevo en unos minutos.", "locked", retry_after=MFA_WINDOW)
        else:
            row, err = _check_assertion(conn, settings, credential, "mfa", account_id=s["id"])
            if err:
                _record(conn, "mfa", key, ip, False)
            else:
                _revoke_one(conn, s["session_id"], "mfa_completed")
                _record(conn, "mfa", key, ip, True)
                out = _finish_login(conn, settings, s, crypto.hash_email(s["email"], _pepper(settings)), ip, ua, mfa=True, method="passkey")
    if err:
        raise err
    return out


# ---------------------------------------------------------------------------------------------------------- entrar sin contraseña
def login_options(settings: Settings, ip: str | None) -> dict:
    _need_enabled(settings)
    rp_id, _, _ = _rp(settings)
    with auth_tx() as conn:
        if ip and _recent(conn, "passkey", ip=ip, seconds=OPTIONS_WINDOW, only_failures=False) >= OPTIONS_PER_IP:
            raise AuthError(429, "Demasiados intentos desde esta red. Inténtalo más tarde.", "rate_limited", retry_after=OPTIONS_WINDOW)
        _record(conn, "passkey", None, ip, True)
        challenge = _issue_challenge(conn, "login")
    return {"options": webauthn.request_options(rp_id=rp_id, challenge=challenge, allow=[])}


def login_finish(settings: Settings, *, credential: dict, ip: str | None, ua: str | None) -> dict:
    _need_enabled(settings)
    err: AuthError | None = None
    out: dict = {}
    with auth_tx() as conn:
        if ip and _recent(conn, "login", ip=ip, seconds=IP_WINDOW, only_failures=True) >= IP_FAIL_LIMIT:
            raise _locked_error(60)
        row, err = _check_assertion(conn, settings, credential, "login")
        if not err and (row["disabled_at"] is not None or row["email_verified_at"] is None or row["password_hash"] == SSO_MARKER):
            err = AuthError(401, "No pudimos iniciar sesión con esa llave de acceso.", "unknown_passkey")
        if err:
            _record(conn, "login", None, ip, False)
        else:
            conn.execute("select set_config('app.current_org', %s, true)", (str(row["organization_id"]),))
            out = _finish_login(conn, settings, row, crypto.hash_email(row["email"], _pepper(settings)), ip, ua, mfa=True, method="passkey_login")
    if err:
        raise err
    return out
