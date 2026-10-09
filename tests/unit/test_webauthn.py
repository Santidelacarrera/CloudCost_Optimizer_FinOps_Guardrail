"""WebAuthn contra un autenticador de software: registro y autenticación con ES256, EdDSA y RS256, y todos los rechazos relevantes."""
from __future__ import annotations

import json
import struct

import pytest
from cloudcost.auth import webauthn as wa
from fake_authenticator import Authenticator, cbor

RP_ID, ORIGIN = "app.acme.com", "https://app.acme.com"
ORIGINS = (ORIGIN,)


def _creation(challenge: bytes | None = None) -> tuple[dict, bytes]:
    ch = challenge or wa.new_challenge()
    return wa.creation_options(rp_id=RP_ID, rp_name="CloudCost", user_id=b"u" * 16, user_name="ana@acme.com", display_name="Ana", challenge=ch, exclude=[]), ch


def _request(cred_id: bytes) -> tuple[dict, bytes]:
    ch = wa.new_challenge()
    return wa.request_options(rp_id=RP_ID, challenge=ch, allow=[(cred_id, ["internal"])]), ch


def _register(auth: Authenticator, **kw) -> wa.NewCredential:
    options, ch = _creation()
    cred = auth.create(options, **kw)
    r = cred["response"]
    return wa.verify_registration(client_data_json=wa.unb64u(r["clientDataJSON"]), attestation_object=wa.unb64u(r["attestationObject"]),
                                  credential_id_b64=cred["id"], challenge=ch, origins=ORIGINS, rp_id=RP_ID)


def _assert(auth: Authenticator, stored: wa.NewCredential, *, stored_count: int | None = None, challenge_override: bytes | None = None, **kw) -> wa.Assertion:
    options, ch = _request(auth.credential_id)
    cred = auth.get(options, **kw)
    r = cred["response"]
    return wa.verify_assertion(
        client_data_json=wa.unb64u(r["clientDataJSON"]), authenticator_data=wa.unb64u(r["authenticatorData"]), signature=wa.unb64u(r["signature"]),
        user_handle=wa.unb64u(r["userHandle"]) if r["userHandle"] else None, challenge=challenge_override or ch, origins=ORIGINS, rp_id=RP_ID,
        public_key=stored.public_key, alg=stored.alg, stored_sign_count=stored.sign_count if stored_count is None else stored_count)


def _err(fn, *a, **k) -> wa.WebAuthnError:
    with pytest.raises(wa.WebAuthnError) as info:
        fn(*a, **k)
    return info.value


# ----------------------------------------------------------------------------------------------------------------- ceremonias felices
@pytest.mark.parametrize("alg", [wa.ALG_ES256, wa.ALG_EDDSA, wa.ALG_RS256])
def test_register_then_authenticate_for_each_algorithm(alg):
    auth = Authenticator(alg, rp_id=RP_ID, origin=ORIGIN)
    stored = _register(auth)
    assert stored.credential_id == auth.credential_id and stored.alg == alg and stored.backup_state is False
    result = _assert(auth, stored)
    assert result.sign_count == 1 and result.user_handle == b"u" * 16


def test_synced_passkey_reports_backup_flags_and_zero_counter():
    auth = Authenticator(rp_id=RP_ID, origin=ORIGIN, synced=True)
    stored = _register(auth)
    assert stored.backup_eligible and stored.backup_state
    assert _assert(auth, stored).sign_count == 0                     # los passkeys sincronizados no llevan contador: no es clonación
    assert _assert(auth, stored).sign_count == 0


def test_options_ask_for_user_verification_and_no_attestation():
    options, _ = _creation()
    assert options["authenticatorSelection"]["userVerification"] == "required" and options["attestation"] == "none"
    assert {p["alg"] for p in options["pubKeyCredParams"]} == {-7, -8, -257}
    req, _ = _request(b"x" * 16)
    assert req["userVerification"] == "required" and req["allowCredentials"][0]["transports"] == ["internal"]


# ----------------------------------------------------------------------------------------------------------------- registro: rechazos
def test_registration_rejects_wrong_origin_rp_and_cross_origin_use():
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), origin="https://evil.example").code == "invalid_origin"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), origin="https://app.acme.com.evil.io").code == "invalid_origin"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), rp_id="evil.example").code == "invalid_rp"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), extra_client={"crossOrigin": True}).code == "invalid_origin"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), extra_client={"topOrigin": "https://otro.example"}).code == "invalid_origin"


def test_registration_requires_presence_and_user_verification():
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), uv=False).code == "user_not_verified"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), up=False).code == "user_not_present"


def test_registration_rejects_wrong_type_challenge_and_credential_id():
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), type_="webauthn.get").code == "invalid_response"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), challenge=wa.b64u(b"otro-challenge-de-32-bytes-xxxxxxx")).code == "invalid_challenge"
    assert _err(_register, Authenticator(rp_id=RP_ID, origin=ORIGIN), id_override=b"otro-id").code == "invalid_response"


def test_registration_rejects_unsupported_keys_and_garbage():
    auth = Authenticator(rp_id=RP_ID, origin=ORIGIN)
    options, ch = _creation()
    cred = auth.create(options)
    r = cred["response"]
    # Curva distinta de P-256 declarada con alg ES256
    bad = auth.cose() | {-1: 2}
    rp_hash = __import__("hashlib").sha256(RP_ID.encode()).digest()
    auth_data = rp_hash + bytes([0x45]) + struct.pack(">I", 0) + b"\0" * 16 + struct.pack(">H", 32) + auth.credential_id + cbor(bad)
    att = cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
    assert _err(wa.verify_registration, client_data_json=wa.unb64u(r["clientDataJSON"]), attestation_object=att, credential_id_b64=cred["id"],
                challenge=ch, origins=ORIGINS, rp_id=RP_ID).code == "unsupported_key"
    for junk in (b"", b"\xff", b"\xbf\x00\x00\xff", cbor([1, 2, 3]), cbor({"fmt": "none"})):          # incluye CBOR indefinido y estructuras erróneas
        assert _err(wa.verify_registration, client_data_json=wa.unb64u(r["clientDataJSON"]), attestation_object=junk, credential_id_b64=cred["id"],
                    challenge=ch, origins=ORIGINS, rp_id=RP_ID).code in ("invalid_response",)


def test_inconsistent_backup_flags_are_rejected():
    rp_hash = __import__("hashlib").sha256(RP_ID.encode()).digest()
    bs_without_be = rp_hash + bytes([0x01 | 0x04 | 0x10]) + struct.pack(">I", 0)
    assert _err(wa.parse_auth_data, bs_without_be).code == "invalid_response"


# ----------------------------------------------------------------------------------------------------------------- autenticación: rechazos
def test_assertion_rejects_bad_signature_other_key_and_tampered_data():
    auth = Authenticator(rp_id=RP_ID, origin=ORIGIN)
    stored = _register(auth)
    other = Authenticator(rp_id=RP_ID, origin=ORIGIN)
    other.credential_id = auth.credential_id
    assert _err(_assert, other, stored).code == "invalid_signature"                     # firma de otra llave
    options, ch = _request(auth.credential_id)
    cred = auth.get(options)
    r = cred["response"]
    tampered = bytearray(wa.unb64u(r["authenticatorData"]))
    tampered[32] |= 0x01
    tampered[36] ^= 0x01                                                                # cambia el contador sin volver a firmar
    assert _err(wa.verify_assertion, client_data_json=wa.unb64u(r["clientDataJSON"]), authenticator_data=bytes(tampered), signature=wa.unb64u(r["signature"]),
                user_handle=None, challenge=ch, origins=ORIGINS, rp_id=RP_ID, public_key=stored.public_key, alg=stored.alg,
                stored_sign_count=0).code == "invalid_signature"


def test_assertion_rejects_wrong_challenge_origin_rp_and_missing_uv():
    auth = Authenticator(rp_id=RP_ID, origin=ORIGIN)
    stored = _register(auth)
    assert _err(_assert, auth, stored, challenge_override=wa.new_challenge()).code == "invalid_challenge"           # respuesta reutilizada con otro challenge
    assert _err(_assert, auth, stored, origin="https://evil.example").code == "invalid_origin"
    assert _err(_assert, auth, stored, rp_id="evil.example").code == "invalid_rp"
    assert _err(_assert, auth, stored, uv=False).code == "user_not_verified"
    assert _err(_assert, auth, stored, up=False).code == "user_not_present"
    assert _err(_assert, auth, stored, extra_client={"crossOrigin": True}).code == "invalid_origin"


def test_sign_counter_must_increase_when_used():
    auth = Authenticator(rp_id=RP_ID, origin=ORIGIN)
    stored = _register(auth)
    assert _assert(auth, stored, count=5).sign_count == 5
    assert _err(_assert, auth, stored, stored_count=5, count=5).code == "counter_regression"        # repetido
    assert _err(_assert, auth, stored, stored_count=9, count=4).code == "counter_regression"        # retrocede: llave clonada
    assert _err(_assert, auth, stored, stored_count=3, count=0).code == "counter_regression"        # pasa a 0 después de haber contado
    assert _assert(auth, stored, stored_count=3, count=4).sign_count == 4


def test_key_type_must_match_stored_algorithm():
    auth = Authenticator(wa.ALG_ES256, rp_id=RP_ID, origin=ORIGIN)
    stored = _register(auth)
    options, ch = _request(auth.credential_id)
    r = auth.get(options)["response"]
    assert _err(wa.verify_assertion, client_data_json=wa.unb64u(r["clientDataJSON"]), authenticator_data=wa.unb64u(r["authenticatorData"]),
                signature=wa.unb64u(r["signature"]), user_handle=None, challenge=ch, origins=ORIGINS, rp_id=RP_ID, public_key=stored.public_key,
                alg=wa.ALG_RS256, stored_sign_count=0).code == "unsupported_key"                  # alg manipulado en la base: no se «degrada»


# ----------------------------------------------------------------------------------------------------------------- CBOR y entradas
def test_cbor_roundtrip_and_limits():
    sample = {"a": [1, -5, 70000, b"xy", "ñ", True, None], 3: {-1: 2}}
    value, end = wa.cbor_decode(cbor(sample))
    assert value == sample and end == len(cbor(sample))
    deep = b"\x81" * 50 + b"\x00"                                                   # 50 listas anidadas
    assert _err(wa.cbor_decode, deep).code == "invalid_response"
    assert _err(wa.cbor_decode, b"\x5f\x41a\xff").code == "invalid_response"        # bytes de longitud indefinida
    assert _err(wa.cbor_decode, b"\x9b\xff\xff\xff\xff\xff\xff\xff\xff").code == "invalid_response"  # lista gigante sobre pocos datos
    assert _err(wa.cbor_decode, b"\xa2\x01\x01\x01\x02").code == "invalid_response"  # clave repetida


def test_client_data_parsing_and_base64_validation():
    assert _err(wa.parse_client_data, b"no es json").code == "invalid_response"
    assert _err(wa.parse_client_data, json.dumps([1]).encode()).code == "invalid_response"
    assert _err(wa.parse_client_data, json.dumps({"type": "x"}).encode()).code == "invalid_response"
    assert _err(wa.parse_client_data, b"x" * (wa.MAX_PAYLOAD + 1)).code == "invalid_response"
    assert _err(wa.unb64u, "no válido!!").code == "invalid_response"
    assert wa.unb64u(wa.b64u(b"\x00\xff\xfe")) == b"\x00\xff\xfe"
    assert len(wa.new_challenge()) == 32 and wa.new_challenge() != wa.new_challenge()


# ----------------------------------------------------------------------------------------------------------------- configuración
def test_relying_party_defaults_come_from_the_public_url_and_are_validated():
    from cloudcost.config import Settings
    s = Settings(env="development", public_web_url="https://app.acme.com")
    assert s.rp_id == "app.acme.com" and s.webauthn_origin_list == ("https://app.acme.com",)
    assert Settings(env="development", public_web_url="http://localhost:5985").webauthn_origin_list == ("http://localhost:5985",)   # con puerto, exacto
    assert Settings(env="development", public_web_url="https://app.acme.com", webauthn_rp_id="acme.com").rp_id == "acme.com"      # dominio superior
    for bad in ({"webauthn_rp_id": "evil.example"}, {"webauthn_rp_id": "cme.com"}, {"webauthn_origins": "https://evil.example"}):
        with pytest.raises(ValueError):
            Settings(env="development", public_web_url="https://app.acme.com", **bad)
    with pytest.raises(ValueError, match="https"):
        Settings(env="production", auth_mode="local", demo_enabled=False, smtp_host="s", auth_pepper="x" * 40,
                 public_web_url="https://app.acme.com", webauthn_origins="http://app.acme.com")
    assert Settings(env="development", passkeys_enabled=False, public_web_url="https://app.acme.com", webauthn_rp_id="evil.example")
