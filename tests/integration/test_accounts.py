"""Cuentas propias contra PostgreSQL real: registro, verificación, bloqueo, 2FA, sesiones, recuperación e invitaciones.

Requiere DATABASE_URL (rol cloudcost_app) y DATABASE_ADMIN_URL (propietario) con las migraciones aplicadas; si no, se omite.
"""
from __future__ import annotations

import os
import re
import unittest
from uuid import uuid4

import pytest

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"
NEW_PASSWORD = "Nube-Verde-Piedra-Sol-81"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


@pytest.fixture
def env():
    _require_db()
    from cloudcost.config import Settings
    from pydantic import SecretStr
    return Settings(env="test", auth_pepper=SecretStr("integration-test-pepper-0123456789abcdef"), public_web_url="http://localhost:3000")


def _admin(sql: str, params=()) -> list[dict]:
    """Consulta como propietario (salta RLS) para preparar o inspeccionar el estado."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _email() -> str:
    return f"t{uuid4().hex[:10]}@example.com"


def _ip() -> str:
    return f"198.51.{uuid4().int % 250}.{uuid4().int % 250 + 1}"


def _token(link: str) -> str:
    return re.search(r"(?:token|invite)=([\w-]+)", link).group(1)


def _principal(settings, token):
    from cloudcost.security import _session_principal
    return _session_principal(token, settings)


def _register(settings, email=None, org="Acme Ltda", name="Ana Pérez"):
    from cloudcost.services import accounts
    email = email or _email()
    outbox: list = []
    out = accounts.signup(settings, email=email, password=PASSWORD, full_name=name, organization_name=org, invite_token=None,
                          ip=_ip(), outbox=outbox)
    return email, out, outbox


def _verified(settings, **kw):
    from cloudcost.services import accounts
    email, out, _ = _register(settings, **kw)
    accounts.verify_email(_token(out["dev_link"]))
    return email


def _login(settings, email, password=PASSWORD, ip="203.0.113.7"):
    from cloudcost.services import accounts
    return accounts.login(settings, email=email, password=password, ip=ip, ua="pytest")


def test_signup_verify_login_and_session(env):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    with pytest.raises(AuthError) as weak:
        accounts.signup(env, email=_email(), password="12345678", full_name="Ana", organization_name="Acme", invite_token=None, ip=None, outbox=[])
    assert weak.value.status == 422 and weak.value.code == "weak_password"

    email, out, outbox = _register(env)
    assert out["status"] == "check_email" and out["dev_link"] and len(outbox) == 1
    with pytest.raises(AuthError) as unverified:
        _login(env, email)
    assert unverified.value.code == "email_not_verified"

    accounts.verify_email(_token(out["dev_link"]))
    with pytest.raises(AuthError):                                    # el enlace sirve una sola vez
        accounts.verify_email(_token(out["dev_link"]))

    session = _login(env, email)
    assert session["status"] == "ok" and session["token"].startswith("ccs_")
    p = _principal(env, session["token"])
    assert p.role == "ADMIN" and p.email == email and p.user_id.startswith("acct:")
    me = accounts.me(p)
    assert me["organization"] == "Acme Ltda" and not me["mfa_enabled"] and me["mfa_recommended"]

    stored = _admin("select token_hash from auth_sessions where account_id = %s", (p.account_id,))
    assert stored and all(session["token"] not in row["token_hash"] for row in stored)       # nunca se guarda el token en claro

    accounts.logout(p.session_id)
    assert accounts.authenticate_session(env, session["token"]) is None


def test_signup_does_not_reveal_existing_accounts(env):
    from cloudcost.services import accounts

    email, _, _ = _register(env)
    outbox: list = []
    again = accounts.signup(env, email=email.upper(), password=PASSWORD, full_name="Otra Persona", organization_name="Otra Org",
                            invite_token=None, ip=_ip(), outbox=outbox)
    assert again == {"status": "check_email"}                           # misma forma, sin enlace
    assert "ya tienes una cuenta" in outbox[0].subject.lower()
    assert _admin("select count(*)::int as n from accounts where email = %s", (email,))[0]["n"] == 1


def test_signup_rate_limit_per_ip(env):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    ip = _ip()
    for _ in range(5):
        accounts.signup(env, email=_email(), password=PASSWORD, full_name="Ana Pérez", organization_name="Acme", invite_token=None, ip=ip, outbox=[])
    with pytest.raises(AuthError) as limited:
        accounts.signup(env, email=_email(), password=PASSWORD, full_name="Ana Pérez", organization_name="Acme", invite_token=None, ip=ip, outbox=[])
    assert limited.value.status == 429


def test_progressive_lockout_same_for_known_and_unknown_emails(env):
    from cloudcost.services.accounts import AuthError

    known = _verified(env)
    unknown = _email()
    for email in (known, unknown):
        for _ in range(5):
            with pytest.raises(AuthError) as bad:
                _login(env, email, "contraseña-incorrecta-1", ip=None)
            assert bad.value.status in (401, 429)
        with pytest.raises(AuthError) as locked:
            _login(env, email, PASSWORD, ip=None)                       # aun con la contraseña correcta
        assert locked.value.status == 429 and locked.value.code == "locked" and locked.value.retry_after

    _admin("update auth_login_attempts set created_at = created_at - interval '1 hour'")
    assert _login(env, known, ip=None)["status"] == "ok"                 # al pasar el bloqueo vuelve a entrar y se reinicia el contador


def test_two_factor_with_replay_protection_and_recovery_codes(env):
    from cloudcost.auth import totp
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    email = _verified(env)
    p = _principal(env, _login(env, email)["token"])
    with pytest.raises(AuthError) as bad_pw:
        accounts.mfa_setup(env, p, "no-es-mi-clave")
    assert bad_pw.value.code == "invalid_password"

    setup = accounts.mfa_setup(env, p, PASSWORD)
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    stored = _admin("select mfa_secret_enc from accounts where id = %s", (p.account_id,))[0]["mfa_secret_enc"]
    assert setup["secret"] not in stored                                # el secreto TOTP está cifrado en reposo

    with pytest.raises(AuthError):
        accounts.mfa_enable(env, p, "000000", [])
    step = totp.current_step()
    enabled = accounts.mfa_enable(env, p, totp.code_at(setup["secret"], step), [])
    codes = enabled["recovery_codes"]
    assert len(codes) == 10

    first = _login(env, email)
    assert first["status"] == "mfa_required" and "token" not in first
    with pytest.raises(AuthError) as wrong:
        accounts.mfa_verify(env, pending_token=first["pending_token"], code="000000", ip=None, ua=None, outbox=[])
    assert wrong.value.code == "invalid_code"

    with pytest.raises(AuthError):                                      # el código ya usado al activar no sirve otra vez
        accounts.mfa_verify(env, pending_token=first["pending_token"], code=totp.code_at(setup["secret"], step), ip=None, ua=None, outbox=[])
    fresh = totp.code_at(setup["secret"], step + 1)
    done = accounts.mfa_verify(env, pending_token=first["pending_token"], code=fresh, ip=None, ua=None, outbox=[])
    assert done["status"] == "ok"
    with pytest.raises(AuthError):                                      # el token pendiente murió; y el mismo código no se reutiliza
        accounts.mfa_verify(env, pending_token=first["pending_token"], code=fresh, ip=None, ua=None, outbox=[])
    second = _login(env, email)
    with pytest.raises(AuthError):
        accounts.mfa_verify(env, pending_token=second["pending_token"], code=fresh, ip=None, ua=None, outbox=[])

    rec = accounts.mfa_verify(env, pending_token=second["pending_token"], code=codes[0].upper(), ip=None, ua=None, outbox=(mails := []))
    assert rec["status"] == "ok" and rec["recovery_codes_left"] == 9 and mails
    third = _login(env, email)
    with pytest.raises(AuthError):                                      # un código de recuperación sirve una sola vez
        accounts.mfa_verify(env, pending_token=third["pending_token"], code=codes[0], ip=None, ua=None, outbox=[])

    with pytest.raises(AuthError):                                      # desactivar exige contraseña Y segundo factor
        accounts.mfa_disable(env, p, PASSWORD, "000000", [])
    accounts.mfa_disable(env, _principal(env, rec["token"]), PASSWORD, codes[1], [])
    assert _login(env, email)["status"] == "ok"


def test_pending_mfa_session_is_not_an_api_session(env):
    from cloudcost.auth import totp
    from cloudcost.services import accounts
    from fastapi import HTTPException

    email = _verified(env)
    p = _principal(env, _login(env, email)["token"])
    setup = accounts.mfa_setup(env, p, PASSWORD)
    accounts.mfa_enable(env, p, totp.code_at(setup["secret"], totp.current_step()), [])
    pending = _login(env, email)["pending_token"]
    with pytest.raises(HTTPException) as exc:
        _principal(env, pending)
    assert exc.value.status_code == 401


def test_sessions_idle_expiry_and_revocation(env):
    from cloudcost.services import accounts

    email = _verified(env)
    a, b = _login(env, email), _login(env, email)
    p = _principal(env, a["token"])
    sessions = accounts.list_sessions(p)
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    assert accounts.revoke_other_sessions(p) == 1
    assert accounts.authenticate_session(env, b["token"]) is None
    assert accounts.authenticate_session(env, a["token"]) is not None

    _admin("update auth_sessions set last_seen_at = now() - interval '5 hours' where account_id = %s", (p.account_id,))
    assert accounts.authenticate_session(env, a["token"]) is None        # inactividad
    c = _login(env, email)
    _admin("update auth_sessions set expires_at = now() - interval '1 minute' where account_id = %s", (p.account_id,))
    assert accounts.authenticate_session(env, c["token"]) is None        # caducidad absoluta


def test_password_reset_revokes_sessions_and_token_is_single_use(env):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    email = _verified(env)
    old = _login(env, email)
    out = accounts.forgot_password(env, email=email, ip=None, outbox=(mails := []))
    assert out["dev_link"] and mails
    assert accounts.forgot_password(env, email=_email(), ip=None, outbox=(none := [])) == {"status": "check_email"} and not none

    token = _token(out["dev_link"])
    with pytest.raises(AuthError) as weak:
        accounts.reset_password(env, token=token, password="12345678", outbox=[])
    assert weak.value.code == "weak_password"
    accounts.reset_password(env, token=token, password=NEW_PASSWORD, outbox=[])
    with pytest.raises(AuthError):
        accounts.reset_password(env, token=token, password=NEW_PASSWORD, outbox=[])
    assert accounts.authenticate_session(env, old["token"]) is None
    with pytest.raises(AuthError):
        _login(env, email, PASSWORD)
    assert _login(env, email, NEW_PASSWORD)["status"] == "ok"


def test_change_password_keeps_current_session_only(env):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    email = _verified(env)
    mine, other = _login(env, email), _login(env, email)
    p = _principal(env, mine["token"])
    with pytest.raises(AuthError):
        accounts.change_password(env, p, "incorrecta-123-ABC", NEW_PASSWORD, [])
    with pytest.raises(AuthError) as same:
        accounts.change_password(env, p, PASSWORD, PASSWORD, [])
    assert same.value.code == "same_password"
    accounts.change_password(env, p, PASSWORD, NEW_PASSWORD, [])
    assert accounts.authenticate_session(env, mine["token"]) is not None
    assert accounts.authenticate_session(env, other["token"]) is None


def test_invitations_members_and_tenant_isolation(env):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    admin_email = _verified(env, org="Org Uno")
    admin = _principal(env, _login(env, admin_email)["token"])
    other_admin = _principal(env, _login(env, _verified(env, org="Org Dos"))["token"])

    invited = _email()
    inv = accounts.invite_member(env, admin, email=invited, role="VIEWER", outbox=(mails := []))
    assert inv["dev_link"] and mails
    token = _token(inv["dev_link"])
    assert [i["email"] for i in accounts.list_invitations(admin.org_id)] == [invited]
    assert accounts.list_invitations(other_admin.org_id) == []

    with pytest.raises(AuthError) as bad:                                # la invitación solo sirve para el correo invitado
        accounts.signup(env, email=_email(), password=PASSWORD, full_name="Intruso", organization_name=None, invite_token=token, ip=None, outbox=[])
    assert bad.value.code == "invalid_invite"
    accounts.signup(env, email=invited, password=PASSWORD, full_name="Beto Soto", organization_name=None, invite_token=token, ip=None, outbox=[])
    with pytest.raises(AuthError):
        accounts.signup(env, email=invited, password=PASSWORD, full_name="Beto Soto", organization_name=None, invite_token=token, ip=None, outbox=[])

    member = _principal(env, _login(env, invited)["token"])             # entra sin verificar correo: la invitación ya lo prueba
    assert member.role == "VIEWER" and str(member.org_id) == str(admin.org_id)

    members = accounts.list_members(admin.org_id)
    assert {m["email"] for m in members} == {admin_email, invited}
    assert all(m["email"] != admin_email for m in accounts.list_members(other_admin.org_id))

    with pytest.raises(AuthError) as selfie:
        accounts.update_member(admin, admin.account_id, role="VIEWER", disabled=None)
    assert selfie.value.code == "self_change"
    with pytest.raises(AuthError):                                       # un admin de otra organización no ve a este miembro
        accounts.update_member(other_admin, member.account_id, role="ADMIN", disabled=None)

    accounts.update_member(admin, member.account_id, role="FINOPS", disabled=None)
    assert _principal(env, _login(env, invited)["token"]).role == "FINOPS"
    old = _login(env, invited)
    accounts.update_member(admin, member.account_id, role=None, disabled=True)
    assert accounts.authenticate_session(env, old["token"]) is None
    with pytest.raises(AuthError):
        _login(env, invited)


def test_audit_chain_stays_valid_and_has_no_secrets(env):
    import json

    from cloudcost.services import accounts

    email = _verified(env)
    p = _principal(env, _login(env, email)["token"])
    accounts.change_password(env, p, PASSWORD, NEW_PASSWORD, [])
    from cloudcost.db import tenant_tx
    with tenant_tx(p.org_id) as conn:
        events = conn.execute("select event_type, payload from audit_events order by seq").fetchall()
        chain = conn.execute("select ok, checked, first_bad_seq from verify_audit_chain()").fetchone()
    assert [e["event_type"] for e in events] == ["ACCOUNT_CREATED", "EMAIL_VERIFIED", "LOGIN_SUCCEEDED", "PASSWORD_CHANGED"]
    assert chain["ok"] is True
    blob = json.dumps(events, default=str)
    assert PASSWORD not in blob and NEW_PASSWORD not in blob and "scrypt$" not in blob


def test_closed_signup_only_accepts_invitations(env):
    from cloudcost.services import accounts
    closed = env.model_copy(update={"auth_signup_open": False})
    with pytest.raises(accounts.AuthError) as e:
        accounts.signup(closed, email=_email(), password=PASSWORD, full_name="Ana Pérez", organization_name="Acme Ltda",
                        invite_token=None, ip=_ip(), outbox=[])
    assert e.value.status == 403 and e.value.code == "signup_closed"
    # una invitación inexistente no abre la puerta: falla por invitación inválida, no por registro cerrado
    with pytest.raises(accounts.AuthError) as e:
        accounts.signup(closed, email=_email(), password=PASSWORD, full_name="Ana Pérez", organization_name=None,
                        invite_token="inventado", ip=_ip(), outbox=[])
    assert e.value.code == "invalid_invite"


def test_operator_reset_mfa_clears_second_factor_closes_sessions_and_audits(env):
    from cloudcost.auth import totp
    from cloudcost.services import accounts

    email = _verified(env)
    session = _login(env, email)["token"]
    p = _principal(env, session)
    setup = accounts.mfa_setup(env, p, PASSWORD)
    accounts.mfa_enable(env, p, totp.code_at(setup["secret"], totp.current_step()), [])
    assert _login(env, email)["status"] == "mfa_required"

    outbox: list = []
    assert accounts.operator_reset_mfa(env, email.upper(), "Identidad verificada por videollamada", outbox) is True

    row = _admin("select mfa_enabled_at, mfa_secret_enc, mfa_last_step from accounts where email = %s", (email,))[0]
    assert row["mfa_enabled_at"] is None and row["mfa_secret_enc"] is None and row["mfa_last_step"] is None
    assert _admin("select count(*) n from auth_recovery_codes where account_id = %s", (p.account_id,))[0]["n"] == 0
    assert _admin("select count(*) n from auth_sessions where account_id = %s and revoked_at is null", (p.account_id,))[0]["n"] == 0
    ev = _admin("select payload from audit_events where entity_id = %s and event_type = 'MFA_DISABLED' order by seq desc limit 1", (str(p.account_id),))[0]["payload"]
    assert ev["via"] == "operator" and "videollamada" in ev["reason"] and ev["had_mfa"] is True
    assert len(outbox) == 1 and "administrador" in outbox[0].text
    assert _login(env, email)["status"] == "ok"                      # ya no pide el segundo factor
    assert accounts.operator_reset_mfa(env, "nadie@example.com", "x", []) is False
