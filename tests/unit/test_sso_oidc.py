"""SSO con OIDC contra un IdP simulado (sin red): descubrimiento, flujo con state/nonce/PKCE, validación del ID token,
identidad y mapeo de roles. El IdP simulado firma con una clave RSA generada en la prueba y expone discovery, JWKS y token endpoint."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cloudcost.auth import oidc
from cloudcost.config import Settings
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fake_idp import CLIENT_ID, ENTRA_ISSUER, OKTA_ISSUER, TENANT, FakeIdP

PEPPER = b"unit-test-pepper-0123456789abcdef-0123"


@pytest.fixture(autouse=True)
def _clean_caches():
    oidc.reset_caches()
    yield
    oidc.reset_caches()


def _cfg(provider: str = "entra", **over) -> oidc.SsoConfig:
    issuer = ENTRA_ISSUER if provider == "entra" else OKTA_ISSUER
    base = dict(provider=provider, issuer=issuer, client_id=CLIENT_ID, client_secret="s3cret", redirect_uri="https://app.acme.com/api/auth/sso/callback",
                tenant_id=TENANT if provider == "entra" else None, role_map={"grp-admins": "ADMIN", "grp-finops": "FINOPS", "grp-viewers": "VIEWER"})
    return oidc.SsoConfig(**{**base, **over})


def _flow(cfg, idp):
    """Recorre el inicio del flujo y devuelve (metadatos, estado, token de flujo, parámetros de la URL)."""
    meta = oidc.discover(cfg, idp)
    url, token = oidc.start_flow(cfg, meta, PEPPER)
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    return meta, oidc.open_flow(token, params["state"], PEPPER), token, params


def _login(cfg, idp, **claim_over):
    meta, flow, _, _ = _flow(cfg, idp)
    idp.id_token = idp.sign(idp.claims(cfg, flow.nonce, **claim_over))
    return oidc.validate_id_token(cfg, meta, oidc.exchange_code(cfg, meta, "code-1", flow.verifier, idp), flow.nonce, idp)


def _err(fn, *args, **kw) -> oidc.SsoError:
    with pytest.raises(oidc.SsoError) as info:
        fn(*args, **kw)
    return info.value


# ------------------------------------------------------------------------------------------------- configuración
def test_role_map_parsing_and_validation():
    assert oidc.parse_role_map("g1:admin, g2:FINOPS") == {"g1": "ADMIN", "g2": "FINOPS"}
    assert oidc.parse_role_map("api://app/Admins:ADMIN") == {"api://app/Admins": "ADMIN"}          # el valor puede contener ':'
    assert oidc.parse_role_map("") == {}
    for bad in ("g1", "g1:SUPERUSER", ":ADMIN", "g1:"):
        with pytest.raises(ValueError):
            oidc.parse_role_map(bad)


def test_issuer_validation():
    assert oidc.validate_issuer("okta", OKTA_ISSUER, None) == []
    assert oidc.validate_issuer("entra", ENTRA_ISSUER, TENANT) == []
    assert oidc.validate_issuer("okta", "http://acme.okta.com", None)                                # http
    assert oidc.validate_issuer("okta", "https://acme.okta.com/?x=1", None)                          # query
    assert oidc.validate_issuer("entra", ENTRA_ISSUER, None)                                         # falta tenant
    assert oidc.validate_issuer("entra", "https://login.microsoftonline.com/common/v2.0", TENANT)    # «common» admite cualquier directorio


GOOD_SSO = dict(sso_enabled=True, sso_provider="okta", sso_issuer=OKTA_ISSUER, sso_client_id="c", sso_client_secret="s",
                sso_org_id="11111111-1111-1111-1111-111111111111", sso_role_map="grp:ADMIN")


def test_settings_accept_valid_sso_and_reject_invalid():
    assert Settings(env="development", **GOOD_SSO).sso_enabled
    for override, fragment in [({"sso_client_secret": ""}, "SSO_CLIENT_ID"), ({"sso_org_id": "no-uuid"}, "SSO_ORG_ID"),
                               ({"sso_role_map": "x:ROOT"}, "SSO_ROLE_MAP"), ({"sso_issuer": "http://acme.okta.com"}, "https"),
                               ({"sso_default_role": "GOD"}, "SSO_DEFAULT_ROLE"), ({"sso_default_role": "", "sso_role_map": ""}, "nadie")]:
        with pytest.raises(ValueError, match=fragment):
            Settings(env="development", **{**GOOD_SSO, **override})


def test_production_with_sso_only_needs_https_and_a_real_pepper():
    prod = dict(env="production", auth_mode="local", auth_local_enabled=False, demo_enabled=False, auth_pepper="x" * 40,
                public_web_url="https://app.acme.com", **GOOD_SSO)
    assert Settings(**prod).sso_enabled                                                              # sin SMTP: no hay cuentas locales
    with pytest.raises(ValueError, match="AUTH_PEPPER"):
        Settings(**{**prod, "auth_pepper": "corto"})
    with pytest.raises(ValueError, match="https"):
        Settings(**{**prod, "public_web_url": "http://app.acme.com"})


# ------------------------------------------------------------------------------------------------- descubrimiento
def test_discovery_is_cached_and_checks_issuer_and_hosts():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    meta = oidc.discover(cfg, idp)
    oidc.discover(cfg, idp)
    assert idp.calls.count(OKTA_ISSUER + "/.well-known/openid-configuration") == 1 and meta.token_endpoint.endswith("/token")

    oidc.reset_caches()
    other = FakeIdP(OKTA_ISSUER)
    other.metadata_overrides = {"issuer": "https://evil.example.com"}
    assert _err(oidc.discover, cfg, other).code == "idp_misconfigured"

    oidc.reset_caches()
    mixup = FakeIdP(OKTA_ISSUER)
    mixup.metadata_overrides = {"token_endpoint": "https://evil.example.com/token"}                   # endpoint en otro host (mix-up)
    assert _err(oidc.discover, cfg, mixup).code == "idp_misconfigured"

    oidc.reset_caches()
    plain = FakeIdP(OKTA_ISSUER)
    plain.metadata_overrides = {"jwks_uri": "http://acme.okta.com/keys"}
    assert _err(oidc.discover, cfg, plain).code == "idp_misconfigured"


def test_discovery_network_failure_is_reported_without_details():
    class Down:
        def get(self, *a, **k):
            raise ConnectionError("dns secreto interno")

    err = _err(oidc.discover, _cfg("okta"), Down())
    assert err.code == "idp_unreachable" and "dns" not in err.message and err.status == 502


# ------------------------------------------------------------------------------------------------- flujo (state / nonce / PKCE)
def test_authorization_url_has_state_nonce_and_s256_pkce():
    cfg, idp = _cfg(), FakeIdP(ENTRA_ISSUER)
    _, flow, _, params = _flow(cfg, idp)
    assert params["response_type"] == "code" and params["client_id"] == CLIENT_ID and params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == cfg.redirect_uri and params["scope"] == "openid email profile"
    assert params["state"] == flow.state and params["nonce"] == flow.nonce
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == expected
    assert "client_secret" not in params and flow.verifier not in json.dumps(params)                 # el verificador no viaja al IdP al inicio


def test_every_flow_is_unique():
    cfg, idp = _cfg(), FakeIdP(ENTRA_ISSUER)
    one, two = _flow(cfg, idp)[1], _flow(cfg, idp)[1]
    assert one.state != two.state and one.nonce != two.nonce and one.verifier != two.verifier


def test_flow_token_rejects_wrong_state_tampering_expiry_and_other_pepper():
    cfg, idp = _cfg(), FakeIdP(ENTRA_ISSUER)
    meta = oidc.discover(cfg, idp)
    url, token = oidc.start_flow(cfg, meta, PEPPER, now=lambda: 1000.0)
    state = parse_qs(urlparse(url).query)["state"][0]
    assert oidc.open_flow(token, state, PEPPER, now=lambda: 1000.0).state == state
    assert _err(oidc.open_flow, token, "otro-state", PEPPER, now=lambda: 1000.0).code == "invalid_flow"      # respuesta inyectada (login CSRF)
    assert _err(oidc.open_flow, token, None, PEPPER, now=lambda: 1000.0).code == "invalid_flow"
    assert _err(oidc.open_flow, None, state, PEPPER, now=lambda: 1000.0).code == "invalid_flow"              # sin cookie de flujo
    assert _err(oidc.open_flow, "basura", state, PEPPER, now=lambda: 1000.0).code == "invalid_flow"
    payload, sig = token.split(".")
    forged = base64.urlsafe_b64encode(json.dumps({"s": state, "n": "x", "v": "y", "e": 9999999999}).encode()).rstrip(b"=").decode()
    assert _err(oidc.open_flow, forged + "." + sig, state, PEPPER, now=lambda: 1000.0).code == "invalid_flow"
    assert _err(oidc.open_flow, token, state, b"otro-secreto-del-servidor-0123456789ab", now=lambda: 1000.0).code == "invalid_flow"
    assert _err(oidc.open_flow, token, state, PEPPER, now=lambda: 1000.0 + oidc.FLOW_TTL + 1).code == "flow_expired"


# ------------------------------------------------------------------------------------------------- canje del código
def test_code_exchange_sends_verifier_and_secret_and_hides_idp_errors():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    claims = _login(cfg, idp)
    sent = idp.token_requests[0]
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "code-1" and sent["code_verifier"] and sent["client_secret"] == "s3cret"
    assert claims["sub"] == "user-123"

    idp.token_status = 400
    meta = oidc.discover(cfg, idp)
    err = _err(oidc.exchange_code, cfg, meta, "x", "v", idp)
    assert err.code == "token_exchange_failed" and "ana@acme.com" not in err.message and "invalid_grant" not in err.message


# ------------------------------------------------------------------------------------------------- ID token
def test_valid_id_token_for_entra_and_okta():
    for provider, issuer in (("entra", ENTRA_ISSUER), ("okta", OKTA_ISSUER)):
        oidc.reset_caches()
        assert _login(_cfg(provider), FakeIdP(issuer))["email"] == "ana@acme.com"


def _validate(cfg, idp, token, nonce):
    return oidc.validate_id_token(cfg, oidc.discover(cfg, idp), token, nonce, idp)


def test_rejects_wrong_nonce_audience_issuer_and_expired():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    now = int(time.time())
    cases = {
        "nonce": idp.claims(cfg, "nonce-ajeno"),
        "aud": idp.claims(cfg, "n", aud="otra-app"),
        "iss": idp.claims(cfg, "n", iss="https://evil.okta.com/oauth2/default"),
        "exp": idp.claims(cfg, "n", exp=now - 3600, iat=now - 7200),
    }
    for name, claims in cases.items():
        err = _err(_validate, cfg, idp, idp.sign(claims), "n")
        assert err.code == "invalid_token", name
    missing = idp.claims(cfg, "n")
    del missing["exp"]
    assert _err(_validate, cfg, idp, idp.sign(missing), "n").code == "invalid_token"


def test_rejects_algorithm_confusion_and_unsigned_tokens():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    claims = idp.claims(cfg, "n")
    public_pem = idp.key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    # Ataque clásico: HS256 firmado con la clave pública (que es pública) como secreto. PyJWT se niega a generarlo, así que se forja a mano.
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT", "kid": idp.kid}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.new(public_pem, header + b"." + body, hashlib.sha256).digest()).rstrip(b"=")
    assert _err(_validate, cfg, idp, (header + b"." + body + b"." + sig).decode(), "n").code == "invalid_token"
    none_header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    assert _err(_validate, cfg, idp, (none_header + b"." + body + b".").decode(), "n").code == "invalid_token"


def test_rejects_token_signed_by_unknown_key_or_with_unknown_kid():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _err(_validate, cfg, idp, idp.sign(idp.claims(cfg, "n"), key=attacker), "n").code == "invalid_token"         # kid conocido, firma ajena
    assert _err(_validate, cfg, idp, idp.sign(idp.claims(cfg, "n"), kid="desconocido"), "n").code == "invalid_token"


def test_key_rotation_refreshes_jwks_once_the_throttle_allows():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    clock = [time.time()]
    meta = oidc.discover(cfg, idp, now=lambda: clock[0])
    oidc.validate_id_token(cfg, meta, idp.sign(idp.claims(cfg, "n")), "n", idp, now=lambda: clock[0])
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated = FakeIdP(OKTA_ISSUER, kid="k2")
    rotated.key = new_key
    idp.extra_keys = [rotated.jwk()]
    token = jwt.encode(idp.claims(cfg, "n"), new_key, algorithm="RS256", headers={"kid": "k2"})
    assert _err(oidc.validate_id_token, cfg, meta, token, "n", idp, now=lambda: clock[0] + 5).code == "invalid_token"   # dentro del tope: no relee
    keys_calls = idp.calls.count("https://acme.okta.com/keys")
    clock[0] += oidc.JWKS_MIN_REFRESH + 1
    assert oidc.validate_id_token(cfg, meta, token, "n", idp, now=lambda: clock[0])["sub"] == "user-123"
    assert idp.calls.count("https://acme.okta.com/keys") == keys_calls + 1


def test_entra_tenant_is_enforced():
    cfg, idp = _cfg("entra"), FakeIdP(ENTRA_ISSUER)
    err = _err(_login, cfg, idp, tid="99999999-9999-9999-9999-999999999999")
    assert err.code == "wrong_tenant" and err.status == 403
    oidc.reset_caches()                                                                              # otra clave: la caché de JWKS es por emisor
    assert _login(cfg, FakeIdP(ENTRA_ISSUER), tid=TENANT.upper())["sub"] == "user-123"


def test_multi_audience_token_requires_matching_azp():
    cfg, idp = _cfg("okta"), FakeIdP(OKTA_ISSUER)
    assert _err(_login, cfg, idp, aud=[CLIENT_ID, "otra"]).code == "invalid_token"
    oidc.reset_caches()
    assert _login(cfg, FakeIdP(OKTA_ISSUER), aud=[CLIENT_ID, "otra"], azp=CLIENT_ID)["sub"] == "user-123"


# ------------------------------------------------------------------------------------------------- identidad y roles
def test_identity_email_fallbacks_and_trust():
    entra = _cfg("entra")
    ident = oidc.identity_from_claims(entra, {"sub": "s", "preferred_username": "Ana.Perez@Acme.com", "roles": ["grp-admins"]})
    assert ident.email == "ana.perez@acme.com" and ident.name == "ana.perez" and ident.email_trusted is False
    assert oidc.identity_from_claims(entra, {"sub": "s", "email": "a@acme.com", "xms_edov": True, "roles": ["grp-admins"]}).email_trusted
    okta = _cfg("okta")
    assert oidc.identity_from_claims(okta, {"sub": "s", "email": "a@acme.com", "email_verified": True, "groups": ["grp-admins"]}).email_trusted
    assert not oidc.identity_from_claims(okta, {"sub": "s", "email": "a@acme.com", "email_verified": "false", "groups": []}).email_trusted
    assert _err(oidc.identity_from_claims, okta, {"sub": "s"}).code == "no_email"
    assert _err(oidc.identity_from_claims, okta, {"sub": "s", "email": "no-es-correo"}).code == "no_email"


def test_domain_allowlist_and_required_amr():
    cfg = _cfg("okta", allowed_domains=("acme.com",), required_amr=("mfa", "hwk"))
    base = {"sub": "s", "email": "a@acme.com", "amr": ["pwd", "mfa"]}
    assert oidc.identity_from_claims(cfg, base).email == "a@acme.com"
    assert _err(oidc.identity_from_claims, cfg, {**base, "email": "a@acme.com.evil.io"}).code == "domain_not_allowed"
    assert _err(oidc.identity_from_claims, cfg, {**base, "email": "a@sub.acme.com"}).code == "domain_not_allowed"
    assert _err(oidc.identity_from_claims, cfg, {**base, "amr": ["pwd"]}).code == "mfa_required"
    assert _err(oidc.identity_from_claims, cfg, {k: v for k, v in base.items() if k != "amr"}).code == "mfa_required"


def test_role_priority_and_default_role():
    cfg = _cfg("okta", default_role="VIEWER")
    assert oidc.role_from_claims(cfg, {"groups": ["grp-viewers", "grp-finops", "grp-admins"]}) == "ADMIN"
    assert oidc.role_from_claims(cfg, {"groups": ["grp-viewers", "grp-finops"]}) == "FINOPS"
    assert oidc.role_from_claims(cfg, {"groups": ["otro"]}) == "VIEWER"
    assert oidc.role_from_claims(cfg, {}) == "VIEWER"
    assert oidc.role_from_claims(cfg, {"groups": "grp-admins"}) == "ADMIN"                           # un solo grupo llega como texto
    strict = _cfg("okta", default_role=None)
    err = _err(oidc.role_from_claims, strict, {"groups": ["otro"]})
    assert err.code == "no_role" and err.status == 403


def test_entra_uses_roles_claim_and_detects_groups_overage():
    entra = _cfg("entra", default_role=None)
    assert oidc.role_from_claims(entra, {"roles": ["grp-finops"]}) == "FINOPS"
    assert _err(oidc.role_from_claims, entra, {"groups": ["grp-admins"]}).code == "no_role"           # Entra: los grupos no se leen si el claim es roles
    groups = _cfg("entra", default_role=None, role_claim="groups")
    assert oidc.role_from_claims(groups, {"groups": ["grp-admins"]}) == "ADMIN"
    overage = {"_claim_names": {"groups": "src1"}}
    assert _err(oidc.role_from_claims, groups, overage).code == "groups_overage"
    assert oidc.role_from_claims(_cfg("entra", role_claim="groups"), overage) == "VIEWER"            # con rol por defecto no se bloquea
