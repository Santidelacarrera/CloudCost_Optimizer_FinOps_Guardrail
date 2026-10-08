"""Rotación del pepper contra PostgreSQL real: contraseñas, TOTP y códigos de recuperación sobreviven y se migran solos.

Requiere DATABASE_URL (rol cloudcost_app) y DATABASE_ADMIN_URL (propietario) con las migraciones aplicadas; si no, se omite.
"""
from __future__ import annotations

import os
import re
import unittest
from uuid import uuid4

import pytest

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"
OLD, NEW = "old-pepper-integration-0123456789abcdef", "new-pepper-integration-fedcba9876543210"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def _settings(*, pepper: str, pepper_id: str = "1", previous: str | None = None):
    from cloudcost.config import Settings
    from pydantic import SecretStr
    return Settings(env="test", public_web_url="http://localhost:3000", auth_pepper=SecretStr(pepper), auth_pepper_id=pepper_id,
                    auth_pepper_previous=SecretStr(previous) if previous else None)


@pytest.fixture
def before():
    _require_db()
    return _settings(pepper=OLD)


@pytest.fixture
def after():
    _require_db()
    return _settings(pepper=NEW, pepper_id="2", previous=f"1:{OLD}")


@pytest.fixture
def only_new():
    _require_db()
    return _settings(pepper=NEW, pepper_id="2")


def _admin(sql: str, params=()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _principal(settings, token):
    from cloudcost.security import _session_principal
    return _session_principal(token, settings)


def _account(settings, *, mfa: bool = False) -> dict:
    """Cuenta verificada (y con 2FA si se pide) creada con la configuración de pepper dada."""
    from cloudcost.auth import totp
    from cloudcost.services import accounts

    email = f"t{uuid4().hex[:10]}@example.com"
    out = accounts.signup(settings, email=email, password=PASSWORD, full_name="Ana Pérez", organization_name="Acme Ltda", invite_token=None,
                          ip=f"198.51.{uuid4().int % 250}.{uuid4().int % 250 + 1}", outbox=[])
    accounts.verify_email(re.search(r"(?:token|invite)=([\w-]+)", out["dev_link"]).group(1))
    info: dict = {"email": email}
    if mfa:
        login = accounts.login(settings, email=email, password=PASSWORD, ip="203.0.113.7", ua="pytest")
        p = _principal(settings, login["token"])
        setup = accounts.mfa_setup(settings, p, PASSWORD)
        step = totp.current_step()
        enabled = accounts.mfa_enable(settings, p, totp.code_at(setup["secret"], step), [])
        info.update(secret=setup["secret"], step=step, codes=enabled["recovery_codes"], account_id=str(p.account_id))
    return info


def _row(email: str) -> dict:
    return _admin("select id, password_hash, mfa_secret_enc from accounts where email = %s", (email,))[0]


def test_la_contrasena_antigua_sigue_valiendo_y_se_rehashea_con_el_pepper_nuevo(before, after, only_new):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    acc = _account(before)
    assert len(_row(acc["email"])["password_hash"].split("$")) == 6           # formato histórico, sin id

    with pytest.raises(AuthError):                                          # mala contraseña: sigue fallando
        accounts.login(after, email=acc["email"], password="Otra-Clave-Larga-9", ip="203.0.113.8", ua="pytest")
    assert accounts.login(after, email=acc["email"], password=PASSWORD, ip="203.0.113.8", ua="pytest")["status"] == "ok"

    migrated = _row(acc["email"])["password_hash"]
    assert migrated.endswith("$2") and len(migrated.split("$")) == 7           # re-hash transparente con el pepper nuevo
    assert accounts.login(after, email=acc["email"], password=PASSWORD, ip="203.0.113.8", ua="pytest")["status"] == "ok"
    assert accounts.login(only_new, email=acc["email"], password=PASSWORD, ip="203.0.113.9", ua="pytest")["status"] == "ok"   # ya sin el viejo


def test_una_cuenta_sin_migrar_no_entra_si_se_retira_el_pepper_viejo(before, only_new):
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError

    acc = _account(before)
    with pytest.raises(AuthError) as err:
        accounts.login(only_new, email=acc["email"], password=PASSWORD, ip="203.0.113.14", ua="pytest")
    assert err.value.code == "invalid_credentials"          # se resuelve con «olvidé mi contraseña»; por eso se mide antes de retirar


def test_el_2fa_sigue_funcionando_y_se_recifra_al_usarlo(before, after, only_new):
    from cloudcost.auth import totp
    from cloudcost.services import accounts

    acc = _account(before, mfa=True)
    assert not _row(acc["email"])["mfa_secret_enc"].startswith("v2.")
    first = accounts.login(after, email=acc["email"], password=PASSWORD, ip="203.0.113.10", ua="pytest")
    assert first["status"] == "mfa_required"
    done = accounts.mfa_verify(after, pending_token=first["pending_token"], code=totp.code_at(acc["secret"], acc["step"] + 1),
                               ip=None, ua=None, outbox=[])
    assert done["status"] == "ok"
    assert _row(acc["email"])["mfa_secret_enc"].startswith("v2.2.")           # re-cifrado con el pepper actual
    second = accounts.login(only_new, email=acc["email"], password=PASSWORD, ip="203.0.113.10", ua="pytest")
    assert accounts.mfa_verify(only_new, pending_token=second["pending_token"], code=totp.code_at(acc["secret"], acc["step"] + 2),
                               ip=None, ua=None, outbox=[])["status"] == "ok"


def test_un_codigo_de_recuperacion_anterior_a_la_rotacion_sigue_valiendo(before, after):
    from cloudcost.services import accounts

    acc = _account(before, mfa=True)
    pending = accounts.login(after, email=acc["email"], password=PASSWORD, ip="203.0.113.11", ua="pytest")["pending_token"]
    out = accounts.mfa_verify(after, pending_token=pending, code=acc["codes"][0], ip=None, ua=None, outbox=[])
    assert out["status"] == "ok" and out["recovery_codes_left"] == 9


def test_los_codigos_nuevos_llevan_el_id_y_funcionan(after):
    from cloudcost.services import accounts

    acc = _account(after, mfa=True)
    stored = _admin("select code_hash from auth_recovery_codes where account_id = %s", (acc["account_id"],))
    assert len(stored) == 10 and all(r["code_hash"].startswith("2:") for r in stored)
    assert _row(acc["email"])["mfa_secret_enc"].startswith("v2.2.")
    pending = accounts.login(after, email=acc["email"], password=PASSWORD, ip="203.0.113.12", ua="pytest")["pending_token"]
    assert accounts.mfa_verify(after, pending_token=pending, code=acc["codes"][3], ip=None, ua=None, outbox=[])["status"] == "ok"


def test_el_comando_recifra_los_totp_sin_perder_el_segundo_factor(before, after, only_new):
    from cloudcost.auth import crypto, rotation, totp
    from cloudcost.services import accounts

    acc = _account(before, mfa=True)
    old_blob = _row(acc["email"])["mfa_secret_enc"]

    dry = rotation._reencrypt(after.pepper_ring, dry_run=True)
    assert dry["to_update"] >= 1 and _row(acc["email"])["mfa_secret_enc"] == old_blob          # el ensayo no escribe

    real = rotation._reencrypt(after.pepper_ring, dry_run=False)
    assert real["updated"] >= 1 and acc["account_id"] not in real["failed_accounts"]
    new_blob = _row(acc["email"])["mfa_secret_enc"]
    assert crypto.secret_pepper_id(new_blob) == "2"
    assert crypto.decrypt_secret(new_blob, only_new.pepper_ring, acc["account_id"]) == acc["secret"]

    # la contraseña se migra al iniciar sesión; después ya se puede retirar el pepper viejo y el 2FA sigue
    accounts.login(after, email=acc["email"], password=PASSWORD, ip="203.0.113.13", ua="pytest")
    pending = accounts.login(only_new, email=acc["email"], password=PASSWORD, ip="203.0.113.13", ua="pytest")
    assert pending["status"] == "mfa_required"
    assert accounts.mfa_verify(only_new, pending_token=pending["pending_token"], code=totp.code_at(acc["secret"], acc["step"] + 1),
                               ip=None, ua=None, outbox=[])["status"] == "ok"


def test_status_cuenta_los_datos_por_pepper(before, after):
    from cloudcost.auth import rotation

    _account(before, mfa=True)
    status = rotation._status(after.pepper_ring)
    assert status["current"] == "2" and status["known"] == ["2", "1"]
    for kind in ("passwords", "totp", "recovery_codes"):
        assert status[kind].get("1", 0) >= 1
    assert rotation.blockers(status, "1") and not rotation.blockers(status, "99")
