"""Cuentas propias: registro, verificación de correo, inicio de sesión, 2FA, sesiones, recuperación e invitaciones.

Principios:
  * Nunca se revela si un correo existe: respuestas genéricas, tiempo parecido (hash de relleno) y los intentos sobre correos
    inexistentes se cuentan igual que los demás.
  * Bloqueo progresivo por cuenta (5 fallos → 30 s, 60 s, 120 s … hasta 15 min) y límites por IP, todo en la base de datos
    (sirve con varias réplicas de la API). El bloqueo no se alarga mientras dura: no sirve para dejar fuera a la víctima.
  * Las sesiones son tokens opacos: en la base solo vive su sha256. Caducan por tiempo absoluto e inactividad y se pueden revocar.
  * Los fallos se confirman (commit) antes de responder el error; por eso las funciones devuelven el error y lo lanzan al salir
    de la transacción.
"""
from __future__ import annotations

import logging
import math
import re
import secrets
import unicodedata
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg import Connection

from ..auth import crypto, mailer, passwords, totp
from ..config import Settings
from ..db import init_pool, tenant_tx
from . import audit

log = logging.getLogger(__name__)

LOCK_AFTER = 5               # fallos seguidos antes del primer bloqueo
LOCK_BASE_SECONDS, LOCK_MAX_SECONDS = 30, 900
IP_FAIL_LIMIT, IP_WINDOW = 30, 900          # fallos de login por IP en 15 min
MFA_FAIL_LIMIT, MFA_WINDOW = 5, 900
MFA_PENDING_SECONDS = 600
MAX_ACTIVE_SESSIONS = 10
SIGNUP_IP_LIMIT, SIGNUP_IP_WINDOW = 5, 3600
MAIL_EMAIL_LIMIT, MAIL_IP_LIMIT, MAIL_WINDOW = 3, 10, 3600
INVITES_PER_HOUR = 20
GENERIC_LOGIN_ERROR = "Correo o contraseña incorrectos."


class AuthError(Exception):
    """Error de autenticación con código HTTP, código estable para la interfaz y, si aplica, segundos de espera."""

    def __init__(self, status: int, message: str, code: str = "error", *, retry_after: int | None = None, errors: list[str] | None = None):
        super().__init__(message)
        self.status, self.message, self.code, self.retry_after, self.errors = status, message, code, retry_after, errors


Outbox = list[mailer.Mail]


# ---------------------------------------------------------------- infraestructura
@contextmanager
def auth_tx(org_id: Any = None) -> Iterator[Connection]:
    """Transacción con el contexto de autenticación (app.auth = 'on'); `org_id` fija además el tenant para auditar."""
    with init_pool().connection() as conn:
        with conn.transaction():
            conn.execute("select set_config('app.auth', 'on', true)")
            if org_id:
                conn.execute("select set_config('app.current_org', %s, true)", (str(org_id),))
            yield conn


def _pepper(settings: Settings) -> bytes:
    return settings.auth_pepper.get_secret_value().encode()


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _web(settings: Settings, path: str) -> str:
    return settings.public_web_url.rstrip("/") + path


def dev_link(settings: Settings, mail: mailer.Mail | None) -> str | None:
    """En desarrollo sin SMTP el enlace se devuelve en la respuesta para poder probar sin correo. Nunca en producción."""
    if mail and not settings.smtp_host and settings.env != "production":
        return mail.link
    return None


def _slug(name: str) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:40] or "org"
    return f"{base}-{secrets.token_hex(3)}"


def _actor(account_id: Any) -> audit.Actor:
    return audit.Actor("user", f"acct:{account_id}")


# ---------------------------------------------------------------- intentos y bloqueo
def _record(conn: Connection, kind: str, email_hash: str | None, ip: str | None, success: bool) -> None:
    conn.execute("insert into auth_login_attempts (kind, email_hash, ip, success) values (%s, %s, %s, %s)", (kind, email_hash, ip, success))
    if secrets.randbelow(100) < 2:       # limpieza ocasional: la tabla no crece sin límite
        conn.execute("delete from auth_login_attempts where created_at < now() - interval '7 days'")
        conn.execute("delete from auth_sessions where expires_at < now() - interval '7 days'")
        conn.execute("delete from auth_tokens where expires_at < now() - interval '7 days'")


def _recent(conn: Connection, kind: str, *, email_hash: str | None = None, ip: str | None = None, seconds: int, only_failures: bool) -> int:
    col, val = ("email_hash", email_hash) if email_hash is not None else ("ip", ip)
    row = conn.execute(
        f"select count(*)::int as n from auth_login_attempts where kind = %s and {col} = %s "
        f"and created_at > now() - make_interval(secs => %s)" + (" and not success" if only_failures else ""),
        (kind, val, seconds)).fetchone()
    return int(row["n"])


def _failures(conn: Connection, kind: str, email_hash: str) -> tuple[int, float]:
    """Fallos desde el último éxito (máx. 24 h) y segundos desde el último fallo."""
    row = conn.execute(
        """select count(*)::int as n, coalesce(extract(epoch from now() - max(created_at)), 0)::float as elapsed
             from auth_login_attempts
            where kind = %s and email_hash = %s and not success and created_at > now() - interval '24 hours'
              and created_at > coalesce((select max(created_at) from auth_login_attempts where kind = %s and email_hash = %s and success),
                                        '-infinity')""",
        (kind, email_hash, kind, email_hash)).fetchone()
    return int(row["n"]), float(row["elapsed"])


def _lock_remaining(conn: Connection, kind: str, email_hash: str) -> int:
    n, elapsed = _failures(conn, kind, email_hash)
    if n < LOCK_AFTER:
        return 0
    wait = min(LOCK_MAX_SECONDS, LOCK_BASE_SECONDS * 2 ** (n - LOCK_AFTER))
    return max(0, math.ceil(wait - elapsed))


def _locked_error(seconds: int) -> AuthError:
    minutes = max(1, math.ceil(seconds / 60))
    when = f"{seconds} segundos" if seconds < 60 else f"{minutes} min"
    return AuthError(429, f"Demasiados intentos. Vuelve a intentarlo en {when}.", "locked", retry_after=seconds)


# ---------------------------------------------------------------- tokens de un solo uso y sesiones
def _issue_token(conn: Connection, purpose: str, hours: int, *, account_id: Any = None, org_id: Any = None, email: str | None = None,
                 role: str | None = None, created_by: Any = None, replace: bool = True) -> str:
    if replace and account_id:
        conn.execute("delete from auth_tokens where account_id = %s and purpose = %s and used_at is null", (str(account_id), purpose))
    raw = crypto.new_token()
    conn.execute(
        """insert into auth_tokens (purpose, token_hash, account_id, organization_id, email, role, created_by, expires_at)
           values (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(hours => %s))""",
        (purpose, crypto.hash_token(raw), str(account_id) if account_id else None, str(org_id) if org_id else None, email, role,
         str(created_by) if created_by else None, hours))
    return raw


def _consume_token(conn: Connection, purpose: str, token: str) -> dict | None:
    """Marca el token como usado de forma atómica; devuelve su fila o None si no existe, venció o ya se usó."""
    return conn.execute(
        """update auth_tokens set used_at = now()
            where token_hash = %s and purpose = %s and used_at is null and expires_at > now()
        returning account_id, organization_id, email, role""", (crypto.hash_token(token), purpose)).fetchone()


def _new_session(conn: Connection, settings: Settings, account_id: Any, org_id: Any, kind: str, ip: str | None, ua: str | None) -> str:
    raw = crypto.new_token(crypto.SESSION_PREFIX)
    ttl = settings.auth_session_hours * 3600 if kind == "active" else MFA_PENDING_SECONDS
    conn.execute(
        """insert into auth_sessions (account_id, organization_id, token_hash, kind, ip, user_agent, expires_at)
           values (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))""",
        (str(account_id), str(org_id), crypto.hash_token(raw), kind, ip, (ua or "")[:300] or None, ttl))
    if kind == "active":
        conn.execute(
            """update auth_sessions set revoked_at = now(), revoked_reason = 'session_limit'
                where account_id = %s and kind = 'active' and revoked_at is null
                  and id not in (select id from auth_sessions where account_id = %s and kind = 'active' and revoked_at is null
                                  order by created_at desc limit %s)""", (str(account_id), str(account_id), MAX_ACTIVE_SESSIONS))
    return raw


def _revoke_sessions(conn: Connection, account_id: Any, reason: str, *, except_id: Any = None) -> int:
    rows = conn.execute(
        """update auth_sessions set revoked_at = now(), revoked_reason = %s
            where account_id = %s and revoked_at is null and (%s::uuid is null or id <> %s::uuid) returning id""",
        (reason, str(account_id), str(except_id) if except_id else None, str(except_id) if except_id else None)).fetchall()
    return len(rows)


def authenticate_session(settings: Settings, token: str) -> dict | None:
    """Valida un token de sesión (vigente, sin revocar, sin exceso de inactividad, cuenta habilitada). Renueva `last_seen_at` ≤ 1/min."""
    with auth_tx() as conn:
        row = conn.execute(
            """select s.id as session_id, s.account_id, s.organization_id, s.kind,
                      a.email, a.full_name, a.role, (a.mfa_enabled_at is not null) as mfa,
                      (s.last_seen_at < now() - interval '60 seconds') as stale
                 from auth_sessions s join accounts a on a.id = s.account_id
                where s.token_hash = %s and s.revoked_at is null and s.expires_at > now()
                  and s.last_seen_at > now() - make_interval(mins => %s) and a.disabled_at is null""",
            (crypto.hash_token(token), settings.auth_session_idle_minutes)).fetchone()
        if row and row["stale"]:
            conn.execute("update auth_sessions set last_seen_at = now() where id = %s", (str(row["session_id"]),))
        return row


# ---------------------------------------------------------------- registro
def _password_errors(settings: Settings, password: str, email: str, name: str) -> list[str]:
    errors = passwords.policy_errors(password, email=email, name=name)
    if not errors and settings.auth_hibp_enabled and passwords.pwned_count(password) > 0:
        errors.append("Esa contraseña aparece en filtraciones conocidas. Elige otra.")
    return errors


def _create_account(conn: Connection, org_id: Any, email: str, name: str, role: str, pw_hash: str, verified: bool) -> str | None:
    row = conn.execute(
        """insert into accounts (organization_id, email, full_name, role, password_hash, email_verified_at)
           values (%s, %s, %s, %s, %s, case when %s then now() end) on conflict (email) do nothing returning id""",
        (str(org_id), email, name, role, pw_hash, verified)).fetchone()
    return str(row["id"]) if row else None


def signup(settings: Settings, *, email: str, password: str, full_name: str, organization_name: str | None, invite_token: str | None,
           ip: str | None, outbox: Outbox) -> dict:
    email = _norm_email(email)
    if not invite_token and not settings.auth_signup_open:
        raise AuthError(403, "El registro está cerrado. Pide una invitación a tu administrador.", "signup_closed")
    errors = _password_errors(settings, password, email, full_name)
    if errors:
        raise AuthError(422, errors[0], "weak_password", errors=errors)
    if not invite_token and not (organization_name and len(organization_name) >= 2):
        raise AuthError(422, "Indica el nombre de tu organización.", "organization_required")
    pepper = _pepper(settings)
    pw_hash = passwords.hash_password(password, pepper)          # siempre, exista o no el correo: mismo costo
    eh = crypto.hash_email(email, pepper)
    out: dict = {"status": "check_email"}
    with auth_tx() as conn:
        if ip and _recent(conn, "signup", ip=ip, seconds=SIGNUP_IP_WINDOW, only_failures=False) >= SIGNUP_IP_LIMIT:
            raise AuthError(429, "Demasiados registros desde esta red. Inténtalo más tarde.", "rate_limited", retry_after=SIGNUP_IP_WINDOW)
        _record(conn, "signup", eh, ip, True)
        existing = conn.execute("select id from accounts where email = %s", (email,)).fetchone()
        if invite_token:
            inv = conn.execute(
                """select id, organization_id, email, role from auth_tokens
                    where token_hash = %s and purpose = 'invite' and used_at is null and expires_at > now() for update""",
                (crypto.hash_token(invite_token),)).fetchone()
            if not inv or inv["email"] != email:
                raise AuthError(400, "La invitación no es válida o ya venció. Pide una nueva a tu administrador.", "invalid_invite")
            if existing:
                raise AuthError(409, "Ya existe una cuenta con este correo. Inicia sesión.", "account_exists")
            conn.execute("select set_config('app.current_org', %s, true)", (str(inv["organization_id"]),))
            acc_id = _create_account(conn, inv["organization_id"], email, full_name, inv["role"], pw_hash, verified=True)
            if not acc_id:
                raise AuthError(503, "No pudimos completar el registro. Inténtalo de nuevo.", "retry")
            conn.execute("update auth_tokens set used_at = now() where id = %s", (str(inv["id"]),))
            audit.record(conn, inv["organization_id"], audit.ACCOUNT_CREATED, actor=_actor(acc_id), entity_type="account",
                         entity_id=acc_id, payload={"via": "invite", "role": inv["role"]})
            return {"status": "created"}
        if existing:
            outbox.append(mailer.already_registered_mail(email, _web(settings, "/login"), _web(settings, "/forgot-password")))
            return out
        org_id = str(uuid.uuid4())
        conn.execute("select set_config('app.current_org', %s, true)", (org_id,))
        conn.execute("insert into organizations (id, name, slug) values (%s, %s, %s)", (org_id, organization_name, _slug(organization_name or "")))
        acc_id = _create_account(conn, org_id, email, full_name, "ADMIN", pw_hash, verified=False)
        if not acc_id:                                      # carrera con otro registro del mismo correo: se deshace también la organización
            raise AuthError(503, "No pudimos completar el registro. Inténtalo de nuevo.", "retry")
        raw = _issue_token(conn, "verify_email", 24, account_id=acc_id, org_id=org_id)
        mail = mailer.verify_email_mail(email, full_name.split()[0], _web(settings, f"/verify-email?token={raw}"))
        outbox.append(mail)
        out["dev_link"] = dev_link(settings, mail)
        audit.record(conn, org_id, audit.ACCOUNT_CREATED, actor=_actor(acc_id), entity_type="account", entity_id=acc_id,
                     payload={"via": "signup", "role": "ADMIN"})
    return out


def verify_email(token: str) -> dict:
    with auth_tx() as conn:
        row = _consume_token(conn, "verify_email", token)
        if not row:
            raise AuthError(400, "El enlace no es válido o ya venció. Pide uno nuevo desde la pantalla de inicio de sesión.", "invalid_token")
        conn.execute("update accounts set email_verified_at = coalesce(email_verified_at, now()) where id = %s", (str(row["account_id"]),))
        conn.execute("select set_config('app.current_org', %s, true)", (str(row["organization_id"]),))
        audit.record(conn, row["organization_id"], audit.EMAIL_VERIFIED, actor=_actor(row["account_id"]), entity_type="account",
                     entity_id=row["account_id"])
    return {"status": "verified"}


def resend_verification(settings: Settings, *, email: str, ip: str | None, outbox: Outbox) -> dict:
    email = _norm_email(email)
    eh = crypto.hash_email(email, _pepper(settings))
    out: dict = {"status": "check_email"}
    with auth_tx() as conn:
        if _recent(conn, "resend", email_hash=eh, seconds=MAIL_WINDOW, only_failures=False) >= MAIL_EMAIL_LIMIT or (
                ip and _recent(conn, "resend", ip=ip, seconds=MAIL_WINDOW, only_failures=False) >= MAIL_IP_LIMIT):
            raise AuthError(429, "Ya enviamos varios correos. Revisa tu bandeja o inténtalo más tarde.", "rate_limited", retry_after=MAIL_WINDOW)
        _record(conn, "resend", eh, ip, True)
        acc = conn.execute("select id, organization_id, full_name from accounts where email = %s and email_verified_at is null "
                           "and disabled_at is null", (email,)).fetchone()
        if acc:
            raw = _issue_token(conn, "verify_email", 24, account_id=acc["id"], org_id=acc["organization_id"])
            mail = mailer.verify_email_mail(email, acc["full_name"].split()[0], _web(settings, f"/verify-email?token={raw}"))
            outbox.append(mail)
            out["dev_link"] = dev_link(settings, mail)
    return out


# ---------------------------------------------------------------- inicio de sesión
def login(settings: Settings, *, email: str, password: str, ip: str | None, ua: str | None) -> dict:
    email = _norm_email(email)
    pepper = _pepper(settings)
    eh = crypto.hash_email(email, pepper)
    with auth_tx() as conn:
        locked = _lock_remaining(conn, "login", eh)
        ip_blocked = bool(ip) and _recent(conn, "login", ip=ip, seconds=IP_WINDOW, only_failures=True) >= IP_FAIL_LIMIT
        acc = conn.execute("select id, organization_id, password_hash, email_verified_at, disabled_at, mfa_enabled_at from accounts where email = %s",
                           (email,)).fetchone()
    if locked or ip_blocked:
        raise _locked_error(locked or 60)
    stored = acc["password_hash"] if acc else passwords.dummy_hash(pepper)
    password_ok = passwords.verify_password(password, stored, pepper)
    ok = bool(acc) and password_ok and acc["disabled_at"] is None
    new_hash = passwords.hash_password(password, pepper) if ok and passwords.needs_rehash(stored) else None
    err: AuthError | None = None
    out: dict = {}
    with auth_tx(acc["organization_id"] if acc else None) as conn:
        if not ok:
            _record(conn, "login", eh, ip, False)
            n, _ = _failures(conn, "login", eh)
            if acc and n == LOCK_AFTER:
                audit.record(conn, acc["organization_id"], audit.LOGIN_LOCKED, actor=_actor(acc["id"]), entity_type="account",
                             entity_id=acc["id"], payload={"failures": n})
            err = _locked_error(_lock_remaining(conn, "login", eh)) if n >= LOCK_AFTER else AuthError(401, GENERIC_LOGIN_ERROR, "invalid_credentials")
        elif acc["email_verified_at"] is None:
            err = AuthError(403, "Confirma tu correo antes de entrar. Revisa tu bandeja o pide un enlace nuevo.", "email_not_verified")
        else:
            if new_hash:
                conn.execute("update accounts set password_hash = %s where id = %s", (new_hash, str(acc["id"])))
            if acc["mfa_enabled_at"] is not None:
                tok = _new_session(conn, settings, acc["id"], acc["organization_id"], "mfa_pending", ip, ua)
                out = {"status": "mfa_required", "pending_token": tok, "expires_in": MFA_PENDING_SECONDS}
            else:
                out = _finish_login(conn, settings, acc, eh, ip, ua, mfa=False)
    if err:
        raise err
    return out


def _finish_login(conn: Connection, settings: Settings, acc: dict, eh: str, ip: str | None, ua: str | None, *, mfa: bool) -> dict:
    tok = _new_session(conn, settings, acc["id"], acc["organization_id"], "active", ip, ua)
    _record(conn, "login", eh, ip, True)                          # un éxito completo reinicia el contador de fallos
    conn.execute("update accounts set last_login_at = now() where id = %s", (str(acc["id"]),))
    audit.record(conn, acc["organization_id"], audit.LOGIN_SUCCEEDED, actor=_actor(acc["id"]), entity_type="account", entity_id=acc["id"],
                 payload={"mfa": mfa, "ip": ip})
    return {"status": "ok", "token": tok, "expires_in": settings.auth_session_hours * 3600}


def _second_factor(conn: Connection, settings: Settings, acc: dict, code: str) -> str | None:
    """Verifica un código TOTP o de recuperación. Devuelve 'totp', 'recovery' o None. TOTP: cada intervalo sirve una sola vez."""
    pepper = _pepper(settings)
    cleaned = (code or "").strip().replace(" ", "")
    if cleaned.isdigit() and len(cleaned) == 6 and acc.get("mfa_secret_enc"):
        secret = crypto.decrypt_secret(acc["mfa_secret_enc"], pepper, str(acc["id"]))
        step = totp.verify(secret, cleaned, last_step=acc.get("mfa_last_step"))
        if step is not None:
            claimed = conn.execute("update accounts set mfa_last_step = %s where id = %s and (mfa_last_step is null or mfa_last_step < %s) "
                                   "returning id", (step, str(acc["id"]), step)).fetchone()
            return "totp" if claimed else None
        return None
    if re.fullmatch(r"[a-z0-9]{5}-?[a-z0-9]{5}", crypto.normalize_recovery(cleaned)):
        norm = crypto.normalize_recovery(cleaned)
        norm = norm if "-" in norm else norm[:5] + "-" + norm[5:]
        used = conn.execute("update auth_recovery_codes set used_at = now() where account_id = %s and code_hash = %s and used_at is null "
                            "returning id", (str(acc["id"]), crypto.hash_recovery(norm, pepper))).fetchone()
        return "recovery" if used else None
    return None


def mfa_verify(settings: Settings, *, pending_token: str, code: str, ip: str | None, ua: str | None, outbox: Outbox) -> dict:
    pepper = _pepper(settings)
    err: AuthError | None = None
    out: dict = {}
    with auth_tx() as conn:
        s = conn.execute(
            """select s.id as session_id, a.id, a.organization_id, a.email, a.mfa_secret_enc, a.mfa_last_step, a.mfa_enabled_at, a.disabled_at
                 from auth_sessions s join accounts a on a.id = s.account_id
                where s.token_hash = %s and s.kind = 'mfa_pending' and s.revoked_at is null and s.expires_at > now() for update of s""",
            (crypto.hash_token(pending_token),)).fetchone()
        if not s or s["disabled_at"] is not None or s["mfa_enabled_at"] is None:
            raise AuthError(401, "La verificación venció. Inicia sesión de nuevo.", "mfa_expired")
        conn.execute("select set_config('app.current_org', %s, true)", (str(s["organization_id"]),))      # para poder auditar
        key = crypto.hash_email(str(s["id"]), pepper)
        if _recent(conn, "mfa", email_hash=key, seconds=MFA_WINDOW, only_failures=True) >= MFA_FAIL_LIMIT:
            _revoke_one(conn, s["session_id"], "mfa_attempts")
            err = AuthError(429, "Demasiados códigos incorrectos. Inicia sesión de nuevo en unos minutos.", "locked", retry_after=MFA_WINDOW)
        else:
            used = _second_factor(conn, settings, s, code)
            if used is None:
                _record(conn, "mfa", key, ip, False)
                _record(conn, "login", crypto.hash_email(s["email"], pepper), ip, False)         # también cuenta para el bloqueo de la cuenta
                err = AuthError(401, "Código incorrecto. Revisa tu aplicación o usa un código de recuperación.", "invalid_code")
            else:
                _revoke_one(conn, s["session_id"], "mfa_completed")
                _record(conn, "mfa", key, ip, True)
                out = _finish_login(conn, settings, s, crypto.hash_email(s["email"], pepper), ip, ua, mfa=True)
                if used == "recovery":
                    left = conn.execute("select count(*)::int as n from auth_recovery_codes where account_id = %s and used_at is null",
                                        (str(s["id"]),)).fetchone()["n"]
                    audit.record(conn, s["organization_id"], audit.RECOVERY_CODE_USED, actor=_actor(s["id"]), entity_type="account",
                                 entity_id=s["id"], payload={"remaining": left})
                    outbox.append(mailer.security_notice_mail(s["email"], f"Entraste con un código de recuperación. Te quedan {left}."))
                    out["recovery_codes_left"] = left
    if err:
        raise err
    return out


def _revoke_one(conn: Connection, session_id: Any, reason: str) -> None:
    conn.execute("update auth_sessions set revoked_at = now(), revoked_reason = %s where id = %s and revoked_at is null", (reason, str(session_id)))


def logout(session_id: str) -> None:
    with auth_tx() as conn:
        _revoke_one(conn, session_id, "logout")


# ---------------------------------------------------------------- perfil y sesiones
def me(p) -> dict:
    with auth_tx(p.org_id) as conn:
        row = conn.execute(
            """select a.id, a.email, a.full_name, a.role, a.email_verified_at, a.mfa_enabled_at, a.password_changed_at, a.last_login_at,
                      a.created_at, o.name as organization, (a.sso_subject is not null) as sso,
                      (select count(*)::int from auth_recovery_codes c where c.account_id = a.id and c.used_at is null) as recovery_codes_left
                 from accounts a join organizations o on o.id = a.organization_id where a.id = %s and a.organization_id = %s""",
            (p.account_id, str(p.org_id))).fetchone()
    if not row:
        raise AuthError(401, "Sesión inválida o expirada", "invalid_session")
    return {"id": str(row["id"]), "email": row["email"], "full_name": row["full_name"], "role": row["role"], "organization": row["organization"],
            "mfa_enabled": row["mfa_enabled_at"] is not None, "recovery_codes_left": row["recovery_codes_left"],
            "email_verified": row["email_verified_at"] is not None, "password_changed_at": row["password_changed_at"],
            "last_login_at": row["last_login_at"], "created_at": row["created_at"],
            "sso": row["sso"],
            "mfa_recommended": row["role"] in ("ADMIN", "FINOPS", "SRE") and row["mfa_enabled_at"] is None and not row["sso"]}


def list_sessions(p) -> list[dict]:
    with auth_tx(p.org_id) as conn:
        rows = conn.execute(
            """select id, ip, user_agent, created_at, last_seen_at, expires_at from auth_sessions
                where account_id = %s and kind = 'active' and revoked_at is null and expires_at > now()
                order by last_seen_at desc""", (p.account_id,)).fetchall()
    return [{**r, "id": str(r["id"]), "current": str(r["id"]) == p.session_id} for r in rows]


def revoke_session(p, session_id: str) -> None:
    with auth_tx(p.org_id) as conn:
        row = conn.execute("update auth_sessions set revoked_at = now(), revoked_reason = 'revoked_by_user' "
                           "where id = %s and account_id = %s and revoked_at is null returning id", (session_id, p.account_id)).fetchone()
        if not row:
            raise AuthError(404, "No encontramos esa sesión.", "not_found")
        audit.record(conn, p.org_id, audit.SESSION_REVOKED, actor=_actor(p.account_id), entity_type="session", entity_id=session_id)


def revoke_other_sessions(p) -> int:
    with auth_tx(p.org_id) as conn:
        n = _revoke_sessions(conn, p.account_id, "revoked_by_user", except_id=p.session_id)
        if n:
            audit.record(conn, p.org_id, audit.SESSION_REVOKED, actor=_actor(p.account_id), entity_type="account",
                         entity_id=p.account_id, payload={"count": n})
    return n


# ---------------------------------------------------------------- reautenticación para acciones sensibles
def _load_account(conn: Connection, p) -> dict:
    row = conn.execute("select id, organization_id, email, full_name, password_hash, mfa_secret_enc, mfa_enabled_at, mfa_last_step "
                       "from accounts where id = %s and organization_id = %s and disabled_at is null for update", (p.account_id, str(p.org_id))).fetchone()
    if not row:
        raise AuthError(401, "Sesión inválida o expirada", "invalid_session")
    return row


def _reauth(conn: Connection, settings: Settings, acc: dict, password: str, code: str | None = None) -> AuthError | None:
    """Contraseña (y segundo factor si la cuenta lo tiene y se pide). Los fallos cuentan para el bloqueo de la cuenta."""
    pepper = _pepper(settings)
    eh = crypto.hash_email(acc["email"], pepper)
    wait = _lock_remaining(conn, "login", eh)
    if wait:
        return _locked_error(wait)
    if not passwords.verify_password(password, acc["password_hash"], pepper):
        _record(conn, "login", eh, None, False)
        return AuthError(403, "La contraseña actual no es correcta.", "invalid_password")
    if code is not None and acc["mfa_enabled_at"] is not None and _second_factor(conn, settings, acc, code) is None:
        _record(conn, "login", eh, None, False)
        return AuthError(403, "El código no es correcto.", "invalid_code")
    return None


def change_password(settings: Settings, p, current: str, new: str, outbox: Outbox) -> None:
    err: AuthError | None = None
    errors = _password_errors(settings, new, p.email or "", p.name or "")
    if errors:
        raise AuthError(422, errors[0], "weak_password", errors=errors)
    new_hash = passwords.hash_password(new, _pepper(settings))
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        err = _reauth(conn, settings, acc, current)
        if not err and passwords.verify_password(new, acc["password_hash"], _pepper(settings)):
            err = AuthError(422, "La nueva contraseña debe ser distinta de la actual.", "same_password")
        if not err:
            conn.execute("update accounts set password_hash = %s, password_changed_at = now() where id = %s", (new_hash, str(acc["id"])))
            _revoke_sessions(conn, acc["id"], "password_changed", except_id=p.session_id)
            audit.record(conn, p.org_id, audit.PASSWORD_CHANGED, actor=_actor(acc["id"]), entity_type="account", entity_id=acc["id"])
            outbox.append(mailer.security_notice_mail(acc["email"], "Cambiaste tu contraseña y cerramos tus otras sesiones."))
    if err:
        raise err


# ---------------------------------------------------------------- recuperación de contraseña
def forgot_password(settings: Settings, *, email: str, ip: str | None, outbox: Outbox) -> dict:
    email = _norm_email(email)
    eh = crypto.hash_email(email, _pepper(settings))
    out: dict = {"status": "check_email"}
    with auth_tx() as conn:
        if _recent(conn, "forgot", email_hash=eh, seconds=MAIL_WINDOW, only_failures=False) >= MAIL_EMAIL_LIMIT or (
                ip and _recent(conn, "forgot", ip=ip, seconds=MAIL_WINDOW, only_failures=False) >= MAIL_IP_LIMIT):
            raise AuthError(429, "Ya enviamos varios correos. Revisa tu bandeja o inténtalo más tarde.", "rate_limited", retry_after=MAIL_WINDOW)
        _record(conn, "forgot", eh, ip, True)
        acc = conn.execute("select id, organization_id from accounts where email = %s and disabled_at is null and sso_subject is null",
                           (email,)).fetchone()                 # las cuentas SSO no tienen contraseña local que recuperar
        if acc:
            raw = _issue_token(conn, "reset_password", 1, account_id=acc["id"], org_id=acc["organization_id"])
            mail = mailer.reset_mail(email, _web(settings, f"/reset-password?token={raw}"))
            outbox.append(mail)
            out["dev_link"] = dev_link(settings, mail)
    return out


def reset_password(settings: Settings, *, token: str, password: str, outbox: Outbox) -> dict:
    pepper = _pepper(settings)
    th = crypto.hash_token(token)
    with auth_tx() as conn:                                        # primero se mira (sin consumir) para validar la política con los datos reales
        row = conn.execute("select t.account_id, a.email, a.full_name from auth_tokens t join accounts a on a.id = t.account_id "
                           "where t.token_hash = %s and t.purpose = 'reset_password' and t.used_at is null and t.expires_at > now()", (th,)).fetchone()
    if not row:
        raise AuthError(400, "El enlace no es válido o ya venció. Pide uno nuevo.", "invalid_token")
    errors = _password_errors(settings, password, row["email"], row["full_name"])
    if errors:
        raise AuthError(422, errors[0], "weak_password", errors=errors)
    new_hash = passwords.hash_password(password, pepper)
    with auth_tx() as conn:
        used = _consume_token(conn, "reset_password", token)
        if not used:
            raise AuthError(400, "El enlace no es válido o ya venció. Pide uno nuevo.", "invalid_token")
        conn.execute("update accounts set password_hash = %s, password_changed_at = now(), email_verified_at = coalesce(email_verified_at, now()) "
                     "where id = %s", (new_hash, str(used["account_id"])))
        _revoke_sessions(conn, used["account_id"], "password_reset")
        _record(conn, "login", crypto.hash_email(row["email"], pepper), None, True)           # levanta un bloqueo por fallos previos
        conn.execute("select set_config('app.current_org', %s, true)", (str(used["organization_id"]),))
        audit.record(conn, used["organization_id"], audit.PASSWORD_RESET, actor=_actor(used["account_id"]), entity_type="account",
                     entity_id=used["account_id"])
        outbox.append(mailer.security_notice_mail(row["email"], "Restableciste tu contraseña y cerramos todas tus sesiones."))
    return {"status": "reset"}


# ---------------------------------------------------------------- 2FA
def mfa_setup(settings: Settings, p, password: str) -> dict:
    err: AuthError | None = None
    out: dict = {}
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        if acc["mfa_enabled_at"] is not None:
            err = AuthError(409, "La verificación en dos pasos ya está activa.", "mfa_already_enabled")
        else:
            err = _reauth(conn, settings, acc, password)
        if not err:
            secret = totp.new_secret()
            conn.execute("update accounts set mfa_secret_enc = %s, mfa_last_step = null where id = %s",
                         (crypto.encrypt_secret(secret, _pepper(settings), str(acc["id"])), str(acc["id"])))
            out = {"secret": secret, "otpauth_uri": totp.otpauth_uri(settings.auth_mfa_issuer, acc["email"], secret)}
    if err:
        raise err
    return out


def _new_recovery_set(conn: Connection, settings: Settings, account_id: Any) -> list[str]:
    codes = crypto.new_recovery_codes()
    conn.execute("delete from auth_recovery_codes where account_id = %s", (str(account_id),))
    for c in codes:
        conn.execute("insert into auth_recovery_codes (account_id, code_hash) values (%s, %s)", (str(account_id), crypto.hash_recovery(c, _pepper(settings))))
    return codes


def mfa_enable(settings: Settings, p, code: str, outbox: Outbox) -> dict:
    err: AuthError | None = None
    out: dict = {}
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        key = crypto.hash_email(str(acc["id"]), _pepper(settings))
        if acc["mfa_enabled_at"] is not None:
            err = AuthError(409, "La verificación en dos pasos ya está activa.", "mfa_already_enabled")
        elif not acc["mfa_secret_enc"]:
            err = AuthError(409, "Primero inicia la configuración para obtener tu código QR.", "mfa_not_started")
        elif _recent(conn, "mfa", email_hash=key, seconds=MFA_WINDOW, only_failures=True) >= MFA_FAIL_LIMIT:
            err = _locked_error(MFA_WINDOW)
        else:
            step = totp.verify(crypto.decrypt_secret(acc["mfa_secret_enc"], _pepper(settings), str(acc["id"])), code)
            if step is None:
                _record(conn, "mfa", key, None, False)
                err = AuthError(422, "El código no coincide. Revisa que la hora de tu teléfono sea automática e inténtalo de nuevo.", "invalid_code")
            else:
                conn.execute("update accounts set mfa_enabled_at = now(), mfa_last_step = %s where id = %s", (step, str(acc["id"])))
                out = {"recovery_codes": _new_recovery_set(conn, settings, acc["id"])}
                _revoke_sessions(conn, acc["id"], "mfa_enabled", except_id=p.session_id)
                audit.record(conn, p.org_id, audit.MFA_ENABLED, actor=_actor(acc["id"]), entity_type="account", entity_id=acc["id"])
                outbox.append(mailer.security_notice_mail(acc["email"], "Activaste la verificación en dos pasos."))
    if err:
        raise err
    return out


def mfa_disable(settings: Settings, p, password: str, code: str, outbox: Outbox) -> None:
    err: AuthError | None = None
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        if acc["mfa_enabled_at"] is None:
            err = AuthError(409, "La verificación en dos pasos no está activa.", "mfa_not_enabled")
        else:
            err = _reauth(conn, settings, acc, password, code)
        if not err:
            conn.execute("update accounts set mfa_secret_enc = null, mfa_enabled_at = null, mfa_last_step = null where id = %s", (str(acc["id"]),))
            conn.execute("delete from auth_recovery_codes where account_id = %s", (str(acc["id"]),))
            _revoke_sessions(conn, acc["id"], "mfa_disabled", except_id=p.session_id)
            audit.record(conn, p.org_id, audit.MFA_DISABLED, actor=_actor(acc["id"]), entity_type="account", entity_id=acc["id"])
            outbox.append(mailer.security_notice_mail(acc["email"], "Desactivaste la verificación en dos pasos."))
    if err:
        raise err


def operator_reset_mfa(settings: Settings, email: str, reason: str, outbox: Outbox) -> bool:
    """Quita el 2FA de una cuenta cuando su dueño perdió el teléfono y los códigos de recuperación. Lo ejecuta quien opera el servidor
    (`python -m cloudcost.cli reset-mfa`) DESPUÉS de verificar la identidad de la persona por otro canal. Cierra todas sus sesiones,
    deja constancia en la auditoría (con el motivo) y avisa a la persona por correo. Devuelve False si no existe la cuenta."""
    email = _norm_email(email)
    with auth_tx() as conn:
        acc = conn.execute("select id, organization_id, email, mfa_enabled_at from accounts where email = %s for update", (email,)).fetchone()
        if not acc:
            return False
        conn.execute("select set_config('app.current_org', %s, true)", (str(acc["organization_id"]),))
        conn.execute("update accounts set mfa_secret_enc = null, mfa_enabled_at = null, mfa_last_step = null where id = %s", (str(acc["id"]),))
        conn.execute("delete from auth_recovery_codes where account_id = %s", (str(acc["id"]),))
        closed = _revoke_sessions(conn, acc["id"], "mfa_reset_by_operator")
        audit.record(conn, acc["organization_id"], audit.MFA_DISABLED, actor=audit.Actor("system", "operator-cli"), entity_type="account",
                     entity_id=acc["id"], payload={"via": "operator", "reason": reason[:300], "had_mfa": acc["mfa_enabled_at"] is not None, "sessions_closed": closed})
        outbox.append(mailer.security_notice_mail(acc["email"], "Un administrador del servicio quitó la verificación en dos pasos de tu cuenta tras verificar tu identidad."))
    return True


def regenerate_recovery_codes(settings: Settings, p, password: str, code: str) -> dict:
    err: AuthError | None = None
    out: dict = {}
    with auth_tx(p.org_id) as conn:
        acc = _load_account(conn, p)
        if acc["mfa_enabled_at"] is None:
            err = AuthError(409, "Activa primero la verificación en dos pasos.", "mfa_not_enabled")
        else:
            err = _reauth(conn, settings, acc, password, code)
        if not err:
            out = {"recovery_codes": _new_recovery_set(conn, settings, acc["id"])}
    if err:
        raise err
    return out


# ---------------------------------------------------------------- equipo (solo ADMIN; corre bajo RLS de la organización)
def list_members(org_id: Any) -> list[dict]:
    with tenant_tx(org_id) as conn:
        return [{**r, "id": str(r["id"])} for r in conn.execute(
            """select id, email, full_name, role, (email_verified_at is not null) as verified, (mfa_enabled_at is not null) as mfa,
                      last_login_at, disabled_at, created_at from accounts order by created_at""").fetchall()]


def update_member(p, member_id: str, *, role: str | None, disabled: bool | None) -> dict:
    if member_id == p.account_id:
        raise AuthError(409, "No puedes cambiar tu propio rol ni deshabilitarte. Pídeselo a otro administrador.", "self_change")
    with tenant_tx(p.org_id) as conn:
        row = conn.execute("select id, role, disabled_at from accounts where id = %s for update", (member_id,)).fetchone()
        if not row:
            raise AuthError(404, "No encontramos a esa persona en tu organización.", "not_found")
        if role is not None:
            conn.execute("update accounts set role = %s where id = %s", (role, member_id))
        if disabled is not None:
            conn.execute("update accounts set disabled_at = case when %s then coalesce(disabled_at, now()) end where id = %s", (disabled, member_id))
            if disabled:
                _revoke_sessions(conn, member_id, "account_disabled")
        audit.record(conn, p.org_id, audit.MEMBER_UPDATED, actor=_actor(p.account_id), entity_type="account", entity_id=member_id,
                     payload={"role": role, "disabled": disabled})
    return {"status": "updated"}


def invite_member(settings: Settings, p, *, email: str, role: str, outbox: Outbox) -> dict:
    email = _norm_email(email)
    with tenant_tx(p.org_id) as conn:
        if conn.execute("select 1 as x from accounts where email = %s", (email,)).fetchone():
            raise AuthError(409, "Esa persona ya forma parte de tu organización.", "already_member")
        recent = conn.execute("select count(*)::int as n from auth_tokens where purpose = 'invite' and created_at > now() - interval '1 hour'").fetchone()["n"]
        if recent >= INVITES_PER_HOUR:
            raise AuthError(429, "Enviaste muchas invitaciones seguidas. Inténtalo más tarde.", "rate_limited", retry_after=3600)
        conn.execute("delete from auth_tokens where purpose = 'invite' and email = %s and used_at is null", (email,))
        raw = _issue_token(conn, "invite", 24 * 7, org_id=p.org_id, email=email, role=role, created_by=p.account_id, replace=False)
        org = conn.execute("select name from organizations where id = %s", (str(p.org_id),)).fetchone()["name"]
        mail = mailer.invite_mail(email, org, p.name or p.email or "Un administrador", _web(settings, f"/signup?invite={raw}"))
        outbox.append(mail)
        audit.record(conn, p.org_id, audit.MEMBER_INVITED, actor=_actor(p.account_id), entity_type="invitation",
                     payload={"role": role, "domain": email.split("@")[-1]})
    return {"status": "invited", "dev_link": dev_link(settings, mail)}


def list_invitations(org_id: Any) -> list[dict]:
    with tenant_tx(org_id) as conn:
        return [{**r, "id": str(r["id"])} for r in conn.execute(
            "select id, email, role, created_at, expires_at from auth_tokens where purpose = 'invite' and used_at is null and expires_at > now() "
            "order by created_at desc").fetchall()]


def revoke_invitation(org_id: Any, invitation_id: str) -> None:
    with tenant_tx(org_id) as conn:
        if not conn.execute("delete from auth_tokens where id = %s and purpose = 'invite' and used_at is null returning id", (invitation_id,)).fetchone():
            raise AuthError(404, "No encontramos esa invitación.", "not_found")
