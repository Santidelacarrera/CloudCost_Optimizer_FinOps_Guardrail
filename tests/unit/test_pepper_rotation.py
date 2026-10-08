"""Rotación del pepper: nada se invalida (contraseñas, TOTP) y el formato histórico no cambia."""
from __future__ import annotations

import pytest
from cloudcost.auth import crypto, passwords, pepper, rotation, totp
from cloudcost.auth.pepper import LEGACY_ID, PepperError, PepperRing

OLD = b"old-pepper-0123456789abcdef-0123456789"
NEW = b"new-pepper-9876543210fedcba-9876543210"
FAST = {"n": 2**10}
ACC = "7c1f2b52-0000-4000-8000-000000000001"


def old_ring() -> PepperRing:
    return PepperRing("1", OLD)


def rotated_ring() -> PepperRing:
    return PepperRing("2", NEW, {"1": OLD})


# ---------------------------------------------------------------- anillo
def test_anillo_basico():
    ring = rotated_ring()
    assert ring.current == NEW and ring.current_id == "2" and ring.ids == ("2", "1")
    assert ring.get("1") == OLD and ring.get("9") is None and ring.is_current("2") and not ring.is_current("1")


@pytest.mark.parametrize("make", [
    lambda: PepperRing("bad id", NEW), lambda: PepperRing("2", NEW, {"2": OLD}), lambda: PepperRing("2", NEW, {"1": NEW}),
    lambda: PepperRing("", NEW), lambda: PepperRing("x" * 17, NEW)])
def test_anillo_invalido(make):
    with pytest.raises(PepperError):
        make()


def test_parse_previous():
    assert pepper.parse_previous(None) == {} and pepper.parse_previous(" , ") == {}
    assert pepper.parse_previous("1:aaa, 2:bb:cc") == {"1": "aaa", "2": "bb:cc"}
    for bad in ("sinseparador", "1:", ":secreto", "1:a,1:b", "x y:a"):
        with pytest.raises(PepperError):
            pepper.parse_previous(bad)


def test_build_ring_desde_texto():
    ring = pepper.build_ring("n" * 40, "2", "1:" + "o" * 40)
    assert ring.ids == ("2", "1") and ring.get("1") == b"o" * 40


# ---------------------------------------------------------------- contraseñas
def test_el_formato_historico_no_cambia_sin_rotar():
    h = passwords.hash_password("clave-larga-de-prueba-1", OLD, **FAST)
    assert len(h.split("$")) == 6 and passwords.pepper_id_of(h) == LEGACY_ID
    assert passwords.verify_password("clave-larga-de-prueba-1", h, OLD)                 # con bytes (API antigua)
    assert passwords.verify_password("clave-larga-de-prueba-1", h, old_ring())


def test_un_hash_antiguo_sigue_valiendo_tras_rotar_y_pide_rehash():
    legacy = passwords.hash_password("clave-larga-de-prueba-2", OLD, **FAST)
    ring = rotated_ring()
    assert passwords.verify_password("clave-larga-de-prueba-2", legacy, ring)
    assert not passwords.verify_password("otra-clave-larga-123", legacy, ring)
    assert passwords.needs_rehash(legacy, ring.current_id)


def test_hash_nuevo_lleva_el_id_y_ya_no_necesita_rehash():
    h = passwords.hash_password("clave-larga-de-prueba-3", NEW, pepper_id="2")
    assert h.endswith("$2") and len(h.split("$")) == 7 and passwords.pepper_id_of(h) == "2"
    assert passwords.verify_password("clave-larga-de-prueba-3", h, rotated_ring())
    assert not passwords.needs_rehash(h, "2")
    assert not passwords.verify_password("clave-larga-de-prueba-3", h, old_ring())      # el anillo viejo no conoce el id 2


def test_pepper_retirado_no_verifica_pero_no_revienta():
    h = passwords.hash_password("clave-larga-de-prueba-4", OLD, **FAST)
    only_new = PepperRing("2", NEW)
    assert passwords.verify_password("clave-larga-de-prueba-4", h, only_new) is False


def test_pepper_retirado_hace_el_mismo_trabajo(monkeypatch):
    calls = []
    real = passwords.hashlib.scrypt
    monkeypatch.setattr(passwords.hashlib, "scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    h = passwords.hash_password("clave-larga-de-prueba-5", OLD, **FAST)
    calls.clear()
    passwords.verify_password("clave-larga-de-prueba-5", h, PepperRing("2", NEW))
    assert calls == [1], "sin el pepper debe hacer igualmente un scrypt (no delatar cuentas sin migrar por tiempo)"


def test_el_relleno_usa_el_pepper_actual_y_nunca_verifica():
    ring = rotated_ring()
    d = passwords.dummy_hash(ring.current, ring.current_id)
    assert passwords.pepper_id_of(d) == "2" and d == passwords.dummy_hash(ring.current, ring.current_id)
    assert not passwords.verify_password("cualquier-cosa-123", d, ring)


@pytest.mark.parametrize("stored", ["", "scrypt$1024$8$3$AA==$AA==$", "scrypt$1024$8$3$AA==$AA==$bad id", "scrypt$1024$8$3$AA==$AA==$2$x",
                                    "scrypt$1$8$3$AA==$AA==", "scrypt$1024$8$99$AA==$AA=="])
def test_hashes_mal_formados(stored):
    assert passwords.verify_password("lo-que-sea-1234", stored, rotated_ring()) is False
    assert passwords.pepper_id_of(stored) is None
    assert passwords.needs_rehash(stored, "2")


# ---------------------------------------------------------------- TOTP
def test_totp_historico_igual_que_antes():
    secret = totp.new_secret()
    blob = crypto.encrypt_secret(secret, OLD, ACC)
    assert not blob.startswith("v2.") and crypto.secret_pepper_id(blob) == LEGACY_ID
    assert crypto.decrypt_secret(blob, OLD, ACC) == secret                                # API antigua con bytes
    assert crypto.decrypt_secret(blob, rotated_ring(), ACC) == secret


def test_totp_nuevo_lleva_el_id_y_se_enlaza_a_la_cuenta():
    secret = totp.new_secret()
    blob = crypto.encrypt_secret(secret, NEW, ACC, "2")
    assert blob.startswith("v2.2.") and crypto.secret_pepper_id(blob) == "2"
    assert crypto.decrypt_secret(blob, rotated_ring(), ACC) == secret
    with pytest.raises(Exception):
        crypto.decrypt_secret(blob, rotated_ring(), "otra-cuenta")


def test_reencrypt_conserva_el_secreto_y_cambia_de_pepper():
    secret = totp.new_secret()
    old_blob = crypto.encrypt_secret(secret, OLD, ACC)
    new_blob = crypto.reencrypt_secret(old_blob, rotated_ring(), ACC)
    assert crypto.secret_pepper_id(new_blob) == "2" and new_blob != old_blob
    assert crypto.decrypt_secret(new_blob, PepperRing("2", NEW), ACC) == secret          # ya no hace falta el pepper viejo


def test_descifrar_con_pepper_retirado_da_error_claro():
    blob = crypto.encrypt_secret(totp.new_secret(), OLD, ACC)
    with pytest.raises(PepperError):
        crypto.decrypt_secret(blob, PepperRing("2", NEW), ACC)


# ---------------------------------------------------------------- códigos de recuperación
def test_recuperacion_historica_igual_que_antes():
    h = crypto.hash_recovery("abcde-fghjk", OLD)
    assert ":" not in h and crypto.recovery_pepper_id(h) == LEGACY_ID and len(h) == 64


def test_un_codigo_antiguo_se_encuentra_tras_rotar_y_uno_nuevo_tambien():
    ring = rotated_ring()
    stored_old = crypto.hash_recovery("abcde-fghjk", OLD)
    stored_new = crypto.hash_recovery("abcde-fghjk", NEW, "2")
    cands = crypto.recovery_candidates("ABCDE FGHJK".replace(" ", "-"), ring)
    assert stored_old in cands and stored_new in cands and stored_new.startswith("2:")
    assert crypto.recovery_pepper_id(stored_new) == "2"
    assert crypto.recovery_candidates("zzzzz-yyyyy", ring)[0] not in (stored_old, stored_new)


# ---------------------------------------------------------------- herramienta
def test_census_y_blockers():
    pw = [passwords.hash_password("clave-larga-de-prueba-6", OLD, **FAST), passwords.hash_password("x" * 14, NEW, pepper_id="2", **FAST), "basura"]
    blobs = [crypto.encrypt_secret("S", OLD, ACC), crypto.encrypt_secret("S", NEW, ACC, "2")]
    codes = [crypto.hash_recovery("abcde-fghjk", OLD), crypto.hash_recovery("abcde-fghjk", NEW, "2")]
    counts = rotation.census(pw, blobs, codes)
    assert counts == {"passwords": {"1": 1, "2": 1, "invalid": 1}, "totp": {"1": 1, "2": 1}, "recovery_codes": {"1": 1, "2": 1}}
    assert rotation.blockers(counts, "1") == {"passwords": 1, "totp": 1, "recovery_codes": 1}
    assert rotation.blockers(counts, "9") == {}
    status = {"current": "2", "known": ["2", "1"], "accounts": 3, **counts}          # lo que devuelve _status: incluye campos que no son contadores
    assert rotation.blockers(status, "1") == {"passwords": 1, "totp": 1, "recovery_codes": 1}


def test_reencrypt_plan():
    ring = rotated_ring()
    secret_a, secret_b = totp.new_secret(), totp.new_secret()
    rows = [
        {"id": "a", "mfa_secret_enc": crypto.encrypt_secret(secret_a, OLD, "a")},
        {"id": "b", "mfa_secret_enc": crypto.encrypt_secret(secret_b, NEW, "b", "2")},
        {"id": "c", "mfa_secret_enc": crypto.encrypt_secret("S", b"pepper-desconocido-0123456789abcdef", "c", "7")},
    ]
    changes, failed, current = rotation.reencrypt_plan(rows, ring)
    assert [c["id"] for c in changes] == ["a"] and failed == ["c"] and current == 1
    assert crypto.decrypt_secret(changes[0]["new"], PepperRing("2", NEW), "a") == secret_a
    assert changes[0]["old"] == rows[0]["mfa_secret_enc"]


# ---------------------------------------------------------------- configuración
GOOD = dict(env="production", auth_mode="local", demo_enabled=False, smtp_host="smtp.example.com",
            public_web_url="https://app.example.com", auth_pepper="n" * 40)


def test_configuracion_por_defecto_es_el_pepper_historico():
    from cloudcost.config import Settings
    ring = Settings(env="development").pepper_ring
    assert ring.current_id == LEGACY_ID and ring.ids == ("1",)


def test_configuracion_de_una_rotacion():
    from cloudcost.config import Settings
    s = Settings(**{**GOOD, "auth_pepper_id": "2", "auth_pepper_previous": "1:" + "o" * 40})
    assert s.pepper_ring.current_id == "2" and s.pepper_ring.get("1") == b"o" * 40


@pytest.mark.parametrize("override,fragment", [
    ({"auth_pepper_id": "2", "auth_pepper_previous": "1:corto"}, "anterior"),
    ({"auth_pepper_id": "2", "auth_pepper_previous": "1:dev-only-" + "x" * 40}, "anterior"),
    ({"auth_pepper_id": "2", "auth_pepper_previous": "sin-formato"}, "AUTH_PEPPER_PREVIOUS"),
    ({"auth_pepper_id": "1", "auth_pepper_previous": "1:" + "o" * 40}, "repetido"),
    ({"auth_pepper_id": "no valido!"}, "inválido"),
    ({"auth_pepper_id": "2", "auth_pepper_previous": "1:" + "n" * 40}, "igual"),
])
def test_configuracion_de_rotacion_invalida(override, fragment):
    from cloudcost.config import Settings
    with pytest.raises(ValueError, match=fragment):
        Settings(**{**GOOD, **override})
