"""Núcleo de seguridad de las cuentas propias: hash de contraseñas, política, TOTP, cifrado y códigos de recuperación."""
from __future__ import annotations

import pytest
from cloudcost.auth import crypto, passwords, totp

PEPPER = b"unit-test-pepper-0123456789abcdef0123"


# ---------------------------------------------------------------- hash
def test_hash_roundtrip_and_format():
    h = passwords.hash_password("correcto caballo bateria", PEPPER, n=2**10)
    assert h.startswith("scrypt$1024$8$3$")
    assert passwords.verify_password("correcto caballo bateria", h, PEPPER)
    assert not passwords.verify_password("correcto caballo bateriA", h, PEPPER)


def test_hash_is_salted_and_depends_on_pepper():
    a = passwords.hash_password("misma-clave-larga-1", PEPPER, n=2**10)
    b = passwords.hash_password("misma-clave-larga-1", PEPPER, n=2**10)
    assert a != b
    assert not passwords.verify_password("misma-clave-larga-1", a, b"otro-pepper-distinto-0123456789abcdef")


def test_unicode_normalization_nfkc():
    composed, decomposed = "contraseña-segura-77", "contraseña-segura-77"
    h = passwords.hash_password(composed, PEPPER, n=2**10)
    assert passwords.verify_password(decomposed, h, PEPPER)


@pytest.mark.parametrize("stored", ["", "x", "bcrypt$1$2$3$a$b", "scrypt$x$8$3$AA==$AA==", "scrypt$1024$8$3$%%%$%%%"])
def test_verify_rejects_malformed_hashes(stored):
    assert passwords.verify_password("lo-que-sea-123", stored, PEPPER) is False


def test_needs_rehash_detects_old_parameters():
    old = passwords.hash_password("clave-larga-de-prueba", PEPPER, n=2**10)
    assert passwords.needs_rehash(old)
    assert passwords.needs_rehash("basura")


def test_default_parameters_are_the_strong_profile():
    h = passwords.hash_password("clave-larga-de-prueba-9", PEPPER)
    assert h.startswith(f"scrypt${2**15}$8$3$")
    assert not passwords.needs_rehash(h)
    assert passwords.verify_password("clave-larga-de-prueba-9", h, PEPPER)


def test_dummy_hash_never_verifies():
    d = passwords.dummy_hash(PEPPER)
    assert d == passwords.dummy_hash(PEPPER)
    assert not passwords.verify_password("cualquier-cosa-123", d, PEPPER)


# ---------------------------------------------------------------- política
@pytest.mark.parametrize("pwd", ["Tr3n-Azul-Lluvia-Cafe", "la lluvia de santiago cae lento", "N7#vq-82Lp!zXc"])
def test_policy_accepts_strong_passwords(pwd):
    assert passwords.policy_errors(pwd, email="ana@example.com", name="Ana Pérez") == []


@pytest.mark.parametrize("pwd,fragment", [
    ("Corta1!", "al menos 12"),
    ("password1234", "común"),
    ("P@ssw0rd2024", "común"),
    ("aaaa1111BBBBcc", "repitas"),
    ("Abcdef12345xyz", "secuencias"),
    ("qwertyuiopAB1", "secuencias"),
    ("solominusculasaqui", "3 tipos"),
])
def test_policy_rejects_weak_passwords(pwd, fragment):
    assert any(fragment in e for e in passwords.policy_errors(pwd)), passwords.policy_errors(pwd)


def test_policy_rejects_personal_data():
    errs = passwords.policy_errors("Marcela-Torres-2031!", email="marcela.torres@example.com", name="Marcela Torres")
    assert any("nombre" in e for e in errs)


def test_policy_limits_length_without_hashing_huge_inputs():
    assert any("máximo" in e for e in passwords.policy_errors("a1B" * 200))


def test_long_passphrase_needs_fewer_character_classes():
    assert passwords.policy_errors("una frase muy larga pero sencilla de recordar") == []


# ---------------------------------------------------------------- TOTP (vectores de RFC 6238, secreto ASCII "12345678901234567890")
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize("t,expected", [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                                        (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")])
def test_totp_rfc6238_vectors(t, expected):
    assert totp.code_at(RFC_SECRET, t // 30, digits=8) == expected


def test_totp_window_and_replay_protection():
    now = 1_700_000_000.0
    step = totp.current_step(now)
    code = totp.code_at(RFC_SECRET, step)
    assert totp.verify(RFC_SECRET, code, now=now) == step
    assert totp.verify(RFC_SECRET, code, now=now + 30) == step           # deriva de reloj de ±1 intervalo
    assert totp.verify(RFC_SECRET, code, now=now + 120) is None          # fuera de ventana
    assert totp.verify(RFC_SECRET, code, now=now, last_step=step) is None   # reutilización rechazada
    assert totp.verify(RFC_SECRET, code, now=now, last_step=step - 1) == step


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5"])
def test_totp_rejects_malformed_codes(bad):
    assert totp.verify(RFC_SECRET, bad, now=1_700_000_000.0) is None


def test_totp_accepts_spaces_in_code():
    now = 1_700_000_000.0
    c = totp.code_at(RFC_SECRET, totp.current_step(now))
    assert totp.verify(RFC_SECRET, c[:3] + " " + c[3:], now=now) is not None


def test_new_secret_and_uri():
    s = totp.new_secret()
    assert len(s) == 32 and s == s.upper()
    uri = totp.otpauth_uri("CloudCost", "ana@example.com", s)
    assert uri.startswith("otpauth://totp/CloudCost%3Aana%40example.com?secret=") and "issuer=CloudCost" in uri


# ---------------------------------------------------------------- cifrado y tokens
def test_secret_encryption_roundtrip_and_binding():
    blob = crypto.encrypt_secret("JBSWY3DPEHPK3PXP", PEPPER, "account-1")
    assert "JBSWY3DPEHPK3PXP" not in blob
    assert crypto.decrypt_secret(blob, PEPPER, "account-1") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(Exception):                       # otra cuenta: el AAD no coincide
        crypto.decrypt_secret(blob, PEPPER, "account-2")
    with pytest.raises(Exception):                       # otro pepper
        crypto.decrypt_secret(blob, b"x" * 40, "account-1")
    assert crypto.encrypt_secret("JBSWY3DPEHPK3PXP", PEPPER, "account-1") != blob      # nonce aleatorio


def test_tokens_are_opaque_and_hash_is_stable():
    t = crypto.new_token(crypto.SESSION_PREFIX)
    assert t.startswith("ccs_") and len(t) > 40
    assert crypto.new_token() != crypto.new_token()
    assert crypto.hash_token(t) == crypto.hash_token(t) and t not in crypto.hash_token(t)


def test_email_hash_is_case_insensitive_and_peppered():
    assert crypto.hash_email(" Ana@Example.com ", PEPPER) == crypto.hash_email("ana@example.com", PEPPER)
    assert crypto.hash_email("ana@example.com", PEPPER) != crypto.hash_email("ana@example.com", b"y" * 40)


def test_recovery_codes():
    codes = crypto.new_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    assert all(len(c) == 11 and c[5] == "-" for c in codes)
    assert crypto.hash_recovery(codes[0].upper(), PEPPER) == crypto.hash_recovery(f" {codes[0]} ", PEPPER)
    assert crypto.hash_recovery(codes[0], PEPPER) != crypto.hash_recovery(codes[1], PEPPER)
