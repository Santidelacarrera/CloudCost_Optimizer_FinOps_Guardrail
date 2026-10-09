"""Llaves de acceso (WebAuthn) contra PostgreSQL real con un autenticador de software: registro, segundo factor, entrada sin contraseña,
reutilización de respuestas, contador, aislamiento entre cuentas y retirada. Requiere DATABASE_URL / DATABASE_ADMIN_URL; si no, se omite."""
from __future__ import annotations

import os
import re
import unittest
from uuid import uuid4

import pytest

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


@pytest.fixture
def env():
    _require_db()
    from cloudcost.config import Settings
    from pydantic import SecretStr
    return Settings(env="test", auth_pepper=SecretStr("integration-test-pepper-0123456789abcdef"), public_web_url="https://app.acme.com")


def _admin(sql: str, params=()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _ip() -> str:
    return f"198.51.{uuid4().int % 250}.{uuid4().int % 250 + 1}"


def _auth(**kw):
    from fake_authenticator import Authenticator
    return Authenticator(rp_id="app.acme.com", origin="https://app.acme.com", **kw)


def _new_account(env):
    """Cuenta verificada con sesión activa. Devuelve (correo, principal)."""
    from cloudcost.security import _session_principal
    from cloudcost.services import accounts
    email = f"p{uuid4().hex[:10]}@example.com"
    out = accounts.signup(env, email=email, password=PASSWORD, full_name="Ana Pérez", organization_name="Acme", invite_token=None, ip=_ip(), outbox=[])
    accounts.verify_email(re.search(r"token=([\w-]+)", out["dev_link"]).group(1))
    token = accounts.login(env, email=email, password=PASSWORD, ip=_ip(), ua="pytest")["token"]
    return email, _session_principal(token, env)


def _add_passkey(env, p, auth, name=None):
    from cloudcost.services import passkeys
    return passkeys.register_finish(env, p, auth.create(passkeys.register_options(env, p, PASSWORD)), name, [])


def _password_login(env, email):
    from cloudcost.services import accounts
    return accounts.login(env, email=email, password=PASSWORD, ip=_ip(), ua="pytest")


def _events(org_id, kind):
    return _admin("select * from audit_events where organization_id = %s and event_type = %s order by created_at", (str(org_id), kind))


def _code(fn, *a, **k) -> str:
    from cloudcost.services.accounts import AuthError
    with pytest.raises(AuthError) as info:
        fn(*a, **k)
    return info.value.code


# -------------------------------------------------------------------------------------------------------------- registro
def test_registration_needs_password_and_stores_only_the_public_key(env):
    from cloudcost.services import accounts, passkeys
    email, p = _new_account(env)
    assert _code(passkeys.register_options, env, p, "contraseña-equivocada") == "invalid_password"

    auth = _auth()
    options = passkeys.register_options(env, p, PASSWORD)
    assert options["rp"]["id"] == "app.acme.com" and options["authenticatorSelection"]["userVerification"] == "required"
    cred = auth.create(options)
    out = passkeys.register_finish(env, p, cred, "  Mi   portátil  ", [])
    assert out["status"] == "added"

    rows = _admin("select * from webauthn_credentials where account_id = %s", (p.account_id,))
    assert len(rows) == 1 and rows[0]["name"] == "Mi portátil" and bytes(rows[0]["credential_id"]) == auth.credential_id
    assert rows[0]["public_key"] and b"PRIVATE" not in bytes(rows[0]["public_key"])
    assert [x["name"] for x in passkeys.list_passkeys(p)] == ["Mi portátil"]
    assert len(_events(p.org_id, "PASSKEY_ADDED")) == 1
    me = accounts.me(p)
    assert me["passkeys"] == 1 and me["mfa_recommended"] is False

    assert _code(passkeys.register_finish, env, p, cred, None, []) == "invalid_challenge"          # el reto ya se consumió: no se repite


def test_same_authenticator_cannot_be_registered_twice_and_challenges_are_bound_to_the_account(env):
    from cloudcost.services import passkeys
    _, p = _new_account(env)
    _, other = _new_account(env)
    auth = _auth()
    _add_passkey(env, p, auth)
    options = passkeys.register_options(env, p, PASSWORD)
    assert [c["id"] for c in options["excludeCredentials"]]                                        # el navegador no ofrecerá la misma llave
    assert _code(passkeys.register_finish, env, p, auth.create(options), None, []) == "passkey_exists"

    # El reto emitido a una cuenta no sirve en otra.
    theirs = passkeys.register_options(env, other, PASSWORD)
    assert _code(passkeys.register_finish, env, p, _auth().create(theirs), None, []) == "invalid_challenge"


def test_registration_rejects_foreign_origin_and_missing_user_verification(env):
    from cloudcost.services import passkeys
    _, p = _new_account(env)
    assert _code(passkeys.register_finish, env, p, _auth().create(passkeys.register_options(env, p, PASSWORD), origin="https://evil.example"), None, []) \
        == "invalid_origin"
    assert _code(passkeys.register_finish, env, p, _auth().create(passkeys.register_options(env, p, PASSWORD), uv=False), None, []) == "user_not_verified"
    assert _admin("select count(*)::int as n from webauthn_credentials where account_id = %s", (p.account_id,))[0]["n"] == 0


def test_passkey_limit_and_sso_accounts(env):
    from cloudcost.services import passkeys
    _, p = _new_account(env)
    for _ in range(passkeys.MAX_PASSKEYS):
        _add_passkey(env, p, _auth())
    assert _code(passkeys.register_options, env, p, PASSWORD) == "too_many_passkeys"
    _, sso = _new_account(env)
    _admin("update accounts set password_hash = '!sso' where id = %s", (sso.account_id,))
    assert _code(passkeys.register_options, env, sso, PASSWORD) == "sso_account"


# -------------------------------------------------------------------------------------------------------------- segundo factor
def test_password_login_then_passkey_completes_the_second_factor(env):
    from cloudcost.security import _session_principal
    from cloudcost.services import passkeys
    email, p = _new_account(env)
    auth = _auth()
    _add_passkey(env, p, auth)

    step = _password_login(env, email)
    assert step["status"] == "mfa_required"
    pending = step["pending_token"]
    offered = passkeys.mfa_options(env, pending)
    assert offered["totp"] is False and len(offered["options"]["allowCredentials"]) == 1
    assert offered["options"]["userVerification"] == "required"

    assertion = auth.get(offered["options"])
    done = passkeys.mfa_finish(env, pending_token=pending, credential=assertion, ip=_ip(), ua="pytest")
    assert done["status"] == "ok" and _session_principal(done["token"], env).email == email
    event = _events(p.org_id, "LOGIN_SUCCEEDED")[-1]
    assert event["payload"]["mfa"] is True and event["payload"]["method"] == "passkey"

    assert _code(passkeys.mfa_finish, env, pending_token=pending, credential=assertion, ip=_ip(), ua="pytest") == "mfa_expired"   # no se repite
    assert _admin("select sign_count, last_used_at from webauthn_credentials where account_id = %s", (p.account_id,))[0]["sign_count"] == 1


def test_a_passkey_from_another_account_cannot_complete_the_second_factor(env):
    from cloudcost.services import passkeys
    email, p = _new_account(env)
    _add_passkey(env, p, _auth())
    _, other = _new_account(env)
    foreign = _auth()
    _add_passkey(env, other, foreign)

    pending = _password_login(env, email)["pending_token"]
    options = passkeys.mfa_options(env, pending)["options"]
    assert _code(passkeys.mfa_finish, env, pending_token=pending, credential=foreign.get(options), ip=_ip(), ua="pytest") == "unknown_passkey"


def test_second_factor_failures_are_limited_per_pending_session(env):
    from cloudcost.services import passkeys
    from cloudcost.services.accounts import MFA_FAIL_LIMIT
    email, p = _new_account(env)
    real = _auth()
    _add_passkey(env, p, real)
    pending = _password_login(env, email)["pending_token"]
    fake = _auth()
    fake.credential_id = real.credential_id                                                        # misma identidad, otra clave: firma inválida
    codes = []
    for _ in range(MFA_FAIL_LIMIT):
        options = passkeys.mfa_options(env, pending)["options"]
        codes.append(_code(passkeys.mfa_finish, env, pending_token=pending, credential=fake.get(options), ip=_ip(), ua="pytest"))
    assert set(codes) == {"invalid_signature"}
    options = passkeys.mfa_options(env, pending)["options"]
    assert _code(passkeys.mfa_finish, env, pending_token=pending, credential=real.get(options), ip=_ip(), ua="pytest") == "locked"


def test_accounts_without_passkeys_enter_directly_and_pending_tokens_are_required(env):
    from cloudcost.services import passkeys
    email, _ = _new_account(env)
    assert _password_login(env, email)["status"] == "ok"                                           # sin llaves ni TOTP: entra directo
    assert _code(passkeys.mfa_options, env, "ccs_no-es-una-sesion-pendiente") == "mfa_expired"


# -------------------------------------------------------------------------------------------------------------- sin contraseña
def test_passwordless_login_with_discoverable_passkey(env):
    from cloudcost.security import _session_principal
    from cloudcost.services import passkeys
    email, p = _new_account(env)
    auth = _auth()
    _add_passkey(env, p, auth)

    options = passkeys.login_options(env, _ip())["options"]
    assert options["allowCredentials"] == [] and options["userVerification"] == "required"
    assertion = auth.get(options)
    out = passkeys.login_finish(env, credential=assertion, ip=_ip(), ua="pytest")
    assert out["status"] == "ok" and _session_principal(out["token"], env).email == email
    assert _events(p.org_id, "LOGIN_SUCCEEDED")[-1]["payload"]["method"] == "passkey_login"
    assert _code(passkeys.login_finish, env, credential=assertion, ip=_ip(), ua="pytest") == "invalid_challenge"      # la misma respuesta no sirve dos veces


def test_passwordless_rejects_unknown_wrong_handle_disabled_and_clone(env):
    from cloudcost.services import passkeys
    email, p = _new_account(env)
    auth = _auth()
    _add_passkey(env, p, auth)

    def attempt(a, **kw):
        return passkeys.login_finish(env, credential=a.get(passkeys.login_options(env, _ip())["options"], **kw), ip=_ip(), ua="pytest")

    assert _code(attempt, _auth()) == "unknown_passkey"                                            # llave que no existe
    assert _code(attempt, auth, user_handle=b"x" * 16) == "unknown_passkey"                        # userHandle de otra persona
    assert attempt(auth, count=10)["status"] == "ok"
    assert _code(attempt, auth, count=10) == "counter_regression"                                  # contador repetido ⇒ posible clon
    assert _code(attempt, auth, count=3) == "counter_regression"
    assert attempt(auth, count=11)["status"] == "ok"

    _admin("update accounts set disabled_at = now() where id = %s", (p.account_id,))
    assert _code(attempt, auth, count=12) == "unknown_passkey"                                     # cuenta deshabilitada


def test_passwordless_is_not_available_for_unverified_or_sso_accounts(env):
    from cloudcost.services import passkeys
    _, p = _new_account(env)
    auth = _auth()
    _add_passkey(env, p, auth)
    _admin("update accounts set email_verified_at = null where id = %s", (p.account_id,))
    assert _code(lambda: passkeys.login_finish(env, credential=auth.get(passkeys.login_options(env, _ip())["options"]), ip=_ip(), ua=None)) == "unknown_passkey"
    _admin("update accounts set email_verified_at = now(), password_hash = '!sso' where id = %s", (p.account_id,))
    assert _code(lambda: passkeys.login_finish(env, credential=auth.get(passkeys.login_options(env, _ip())["options"], count=5), ip=_ip(), ua=None)) \
        == "unknown_passkey"


def test_challenge_issuance_is_rate_limited_per_ip(env):
    from cloudcost.services import passkeys
    ip = _ip()
    for _ in range(passkeys.OPTIONS_PER_IP):
        passkeys.login_options(env, ip)
    assert _code(passkeys.login_options, env, ip) == "rate_limited"
    passkeys.login_options(env, _ip())                                                              # otra IP no se ve afectada


# -------------------------------------------------------------------------------------------------------------- retirada y operador
def test_removing_a_passkey_needs_password_closes_other_sessions_and_restores_plain_login(env):
    from cloudcost.services import accounts, passkeys
    email, p = _new_account(env)
    auth = _auth()
    _add_passkey(env, p, auth)
    pending = _password_login(env, email)["pending_token"]
    other = passkeys.mfa_finish(env, pending_token=pending, credential=auth.get(passkeys.mfa_options(env, pending)["options"]), ip=_ip(), ua="pytest")["token"]
    assert accounts.authenticate_session(env, other) is not None
    pid = passkeys.list_passkeys(p)[0]["id"]

    assert _code(passkeys.remove_passkey, env, p, pid, "mal-la-contraseña", []) == "invalid_password"
    assert _code(passkeys.remove_passkey, env, p, str(uuid4()), PASSWORD, []) == "not_found"
    passkeys.remove_passkey(env, p, pid, PASSWORD, [])
    assert passkeys.list_passkeys(p) == [] and len(_events(p.org_id, "PASSKEY_REMOVED")) == 1
    assert accounts.authenticate_session(env, other) is None                                        # la otra sesión se cerró
    assert _password_login(env, email)["status"] == "ok"                                           # sin llaves vuelve a entrar con la contraseña sola


def test_operator_reset_mfa_also_removes_passkeys(env):
    from cloudcost.services import accounts
    email, p = _new_account(env)
    _add_passkey(env, p, _auth())
    assert accounts.operator_reset_mfa(env, email, "verificado por videollamada", []) is True
    assert _admin("select count(*)::int as n from webauthn_credentials where account_id = %s", (p.account_id,))[0]["n"] == 0
    assert _password_login(env, email)["status"] == "ok"                                           # ya no exige una llave que se perdió


def test_row_level_security_hides_other_tenants_credentials(env):
    """Con el contexto de un tenant solo se ven las llaves de su organización (RLS FORCE)."""
    from cloudcost.db import tenant_tx
    _, a = _new_account(env)
    _, b = _new_account(env)
    _add_passkey(env, a, _auth())
    _add_passkey(env, b, _auth())
    with tenant_tx(a.org_id) as conn:
        rows = conn.execute("select account_id from webauthn_credentials").fetchall()
    assert {str(r["account_id"]) for r in rows} == {a.account_id}
