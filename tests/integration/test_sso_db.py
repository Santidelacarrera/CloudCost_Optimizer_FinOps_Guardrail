"""SSO contra PostgreSQL real, con un IdP simulado: alta automática, rol desde grupos, enlace de cuentas, anti-nOAuth,
cuentas deshabilitadas y auditoría. Requiere DATABASE_URL / DATABASE_ADMIN_URL con las migraciones aplicadas; si no, se omite."""
from __future__ import annotations

import os
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def _admin(sql: str, params=()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def org():
    _require_db()
    org_id = str(uuid4())
    _admin("insert into organizations (id, name, slug) values (%s, %s, %s)", (org_id, "Acme SSO", f"acme-{org_id[:8]}"))
    return org_id


def _settings(org_id: str, **over):
    from cloudcost.config import Settings
    from fake_idp import CLIENT_ID, OKTA_ISSUER
    from pydantic import SecretStr
    base = dict(env="test", auth_pepper=SecretStr("integration-test-pepper-0123456789abcdef"), public_web_url="http://localhost:3000",
                sso_enabled=True, sso_provider="okta", sso_issuer=OKTA_ISSUER, sso_client_id=CLIENT_ID, sso_client_secret=SecretStr("s3cret"),
                sso_org_id=org_id, sso_role_map="grp-admins:ADMIN,grp-finops:FINOPS", sso_default_role="VIEWER")
    return Settings(**{**base, **over})


@pytest.fixture
def idp():
    from cloudcost.auth import oidc
    from fake_idp import OKTA_ISSUER, FakeIdP
    oidc.reset_caches()
    yield FakeIdP(OKTA_ISSUER)
    oidc.reset_caches()


def _ip() -> str:
    return f"198.51.{uuid4().int % 250}.{uuid4().int % 250 + 1}"


def _sso_login(settings, idp, *, sub="user-1", email=None, **claims):
    """Recorre start → IdP → callback y devuelve la respuesta del servicio (o lanza AuthError)."""
    from cloudcost.services import sso
    start = sso.start(settings, http=idp)
    params = {k: v[0] for k, v in parse_qs(urlparse(start["authorize_url"]).query).items()}
    cfg = sso.config_from_settings(settings)
    idp.id_token = idp.sign(idp.claims(cfg, params["nonce"], sub=sub, email=email or f"{sub}@acme.com", **claims))
    return sso.callback(settings, code="code-1", state=params["state"], flow_token=start["flow_token"], ip=_ip(), ua="pytest", http=idp)


def _account(email: str) -> dict | None:
    rows = _admin("select * from accounts where email = %s", (email,))
    return rows[0] if rows else None


def _events(org_id: str, kind: str) -> list[dict]:
    return _admin("select * from audit_events where organization_id = %s and event_type = %s order by created_at", (org_id, kind))


def _principal(settings, token):
    from cloudcost.security import _session_principal
    return _session_principal(token, settings)


def test_first_login_creates_account_with_role_from_groups(org, idp):
    s = _settings(org)
    out = _sso_login(s, idp, sub="ana", groups=["grp-finops"], email_verified=True)
    assert out["status"] == "ok" and out["token"].startswith("ccs_")
    acc = _account("ana@acme.com")
    assert acc["role"] == "FINOPS" and acc["password_hash"] == "!sso" and acc["sso_subject"] == "ana" and acc["email_verified_at"] is not None
    p = _principal(s, out["token"])
    assert p.role == "FINOPS" and str(p.org_id) == org and p.email == "ana@acme.com"
    assert len(_events(org, "ACCOUNT_CREATED")) == 1 and len(_events(org, "LOGIN_SUCCEEDED")) == 1
    login_event = _events(org, "LOGIN_SUCCEEDED")[0]
    assert login_event["payload"]["sso"] == "okta" and "ana@acme.com" not in str(login_event["payload"])      # el correo no va en la auditoría


def test_no_matching_group_gets_default_role_or_is_rejected(org, idp):
    from cloudcost.services.accounts import AuthError
    assert _principal(_settings(org), _sso_login(_settings(org), idp, sub="v1", groups=["otro"])["token"]).role == "VIEWER"
    strict = _settings(org, sso_default_role="")
    with pytest.raises(AuthError) as err:
        _sso_login(strict, idp, sub="v2", groups=["otro"])
    assert err.value.code == "no_role" and _account("v2@acme.com") is None


def test_returning_login_reuses_account_and_syncs_role(org, idp):
    s = _settings(org)
    first = _sso_login(s, idp, sub="bea", groups=["grp-admins"])
    assert _account("bea@acme.com")["role"] == "ADMIN"
    _sso_login(s, idp, sub="bea", groups=["grp-finops"])                             # la quitaron del grupo de administradores
    rows = _admin("select * from accounts where sso_subject = 'bea'")
    assert len(rows) == 1 and rows[0]["role"] == "FINOPS" and rows[0]["last_login_at"] is not None
    changes = _events(org, "MEMBER_UPDATED")
    assert changes and changes[-1]["payload"]["previous_role"] == "ADMIN"
    assert _principal(s, first["token"]).role == "FINOPS"                           # la sesión anterior también ve el rol vigente


def test_existing_local_email_is_not_taken_over_by_default(org, idp):
    from cloudcost.auth import passwords
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError
    pepper = b"integration-test-pepper-0123456789abcdef"
    _admin("insert into accounts (organization_id, email, full_name, role, password_hash, email_verified_at) values (%s, %s, 'Local', 'ADMIN', %s, now())",
           (org, "local@acme.com", passwords.hash_password(PASSWORD, pepper)))
    s = _settings(org)
    with pytest.raises(AuthError) as err:
        _sso_login(s, idp, sub="intruso", email="local@acme.com", email_verified=True, groups=["grp-admins"])
    assert err.value.code == "account_exists" and err.value.status == 409
    acc = _account("local@acme.com")
    assert acc["sso_subject"] is None and acc["password_hash"] != "!sso"             # la cuenta local sigue intacta
    assert accounts.login(s, email="local@acme.com", password=PASSWORD, ip=_ip(), ua=None)["status"] == "ok"


def test_linking_requires_explicit_opt_in_trusted_email_and_same_org(org, idp):
    from cloudcost.auth import passwords
    from cloudcost.services import accounts
    from cloudcost.services.accounts import AuthError
    pepper = b"integration-test-pepper-0123456789abcdef"
    _admin("insert into accounts (organization_id, email, full_name, role, password_hash, email_verified_at) values (%s, %s, 'Local', 'VIEWER', %s, now())",
           (org, "link@acme.com", passwords.hash_password(PASSWORD, pepper)))
    old = accounts.login(_settings(org), email="link@acme.com", password=PASSWORD, ip=_ip(), ua=None)["token"]
    s = _settings(org, sso_link_existing=True)
    with pytest.raises(AuthError):                                                   # el IdP no afirma que verificó el correo
        _sso_login(s, idp, sub="l1", email="link@acme.com", groups=["grp-finops"])
    assert _account("link@acme.com")["sso_subject"] is None
    _sso_login(s, idp, sub="l1", email="link@acme.com", email_verified=True, groups=["grp-finops"])
    acc = _account("link@acme.com")
    assert acc["sso_subject"] == "l1" and acc["password_hash"] == "!sso" and acc["role"] == "FINOPS"
    assert len(_events(org, "ACCOUNT_LINKED")) == 1
    with pytest.raises(AuthError) as err:                                            # ya sin contraseña local
        accounts.login(s, email="link@acme.com", password=PASSWORD, ip=_ip(), ua=None)
    assert err.value.code == "invalid_credentials"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):                                               # y su sesión anterior se cerró al enlazar
        _principal(s, old)


def test_same_email_with_different_subject_cannot_take_an_sso_account(org, idp):
    """nOAuth: en Entra el correo es editable; otro `sub` con el mismo correo no hereda la cuenta aunque el enlace esté permitido."""
    from cloudcost.services.accounts import AuthError
    s = _settings(org, sso_link_existing=True)
    _sso_login(s, idp, sub="dueno", email="ceo@acme.com", email_verified=True, groups=["grp-admins"])
    with pytest.raises(AuthError) as err:
        _sso_login(s, idp, sub="atacante", email="ceo@acme.com", email_verified=True, groups=["grp-admins"])
    assert err.value.code == "account_exists"
    assert _account("ceo@acme.com")["sso_subject"] == "dueno"


def test_disabled_account_and_no_jit(org, idp):
    from cloudcost.services.accounts import AuthError
    s = _settings(org)
    _sso_login(s, idp, sub="baja", groups=["grp-finops"])
    _admin("update accounts set disabled_at = now() where sso_subject = 'baja'")
    with pytest.raises(AuthError) as err:
        _sso_login(s, idp, sub="baja", groups=["grp-finops"])
    assert err.value.code == "account_disabled"
    closed = _settings(org, sso_jit=False)
    with pytest.raises(AuthError) as err2:
        _sso_login(closed, idp, sub="nueva", groups=["grp-finops"])
    assert err2.value.code == "not_provisioned" and _account("nueva@acme.com") is None


def test_rejected_logins_are_audited_without_personal_data(org, idp):
    from cloudcost.services.accounts import AuthError
    s = _settings(org, sso_allowed_domains="acme.com")
    with pytest.raises(AuthError) as err:
        _sso_login(s, idp, sub="x", email="x@otra.com")
    assert err.value.code == "domain_not_allowed"
    failures = _events(org, "SSO_LOGIN_FAILED")
    assert failures and failures[-1]["payload"]["code"] == "domain_not_allowed" and "otra.com" not in str(failures[-1]["payload"])
    assert _account("x@otra.com") is None


def test_callback_without_the_browsers_flow_cookie_is_rejected(org, idp):
    from cloudcost.services import sso
    from cloudcost.services.accounts import AuthError
    s = _settings(org)
    start = sso.start(s, http=idp)
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    other = sso.start(s, http=idp)["flow_token"]                                     # cookie de otro inicio de sesión (atacante inyecta su código)
    with pytest.raises(AuthError) as err:
        sso.callback(s, code="x", state=state, flow_token=other, ip=_ip(), ua=None, http=idp)
    assert err.value.code == "invalid_flow"
    assert not _events(org, "SSO_LOGIN_FAILED")                                      # sin flujo válido no se escribe en la auditoría


def test_sso_accounts_get_no_password_recovery_mail(org, idp):
    from cloudcost.services import accounts
    s = _settings(org)
    _sso_login(s, idp, sub="rec", groups=["grp-finops"])
    outbox: list = []
    assert accounts.forgot_password(s, email="rec@acme.com", ip=_ip(), outbox=outbox)["status"] == "check_email"
    assert outbox == []
    assert _admin("select count(*)::int as n from auth_tokens where purpose = 'reset_password' and account_id = %s", (_account("rec@acme.com")["id"],))[0]["n"] == 0
