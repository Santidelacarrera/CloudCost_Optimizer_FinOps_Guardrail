"""Hash y política de contraseñas.

Hash: scrypt (N=2^15, r=8, p=3: perfil OWASP de 32 MiB) sobre HMAC-SHA256(pepper, contraseña normalizada NFKC). El pepper vive en el
servidor (no en la base), así que un volcado de la tabla `accounts` por sí solo no basta para atacar las contraseñas offline.
El formato es autodescriptivo (`scrypt$n$r$p$sal$hash`) para poder subir los parámetros más adelante y rehashear al iniciar sesión.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import unicodedata
import urllib.request

SCRYPT_N, SCRYPT_R, SCRYPT_P, DK_LEN = 2**15, 8, 3, 32
MIN_LENGTH, MAX_LENGTH = 12, 256
_slots = threading.BoundedSemaphore(4)       # como mucho 4 scrypt a la vez (32 MiB cada uno): un ataque de volumen no agota la memoria


def _prehash(password: str, pepper: bytes) -> bytes:
    return hmac.new(pepper, unicodedata.normalize("NFKC", password).encode("utf-8"), hashlib.sha256).digest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, pepper: bytes, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    with _slots:
        dk = hashlib.scrypt(_prehash(password, pepper), salt=salt, n=n, r=r, p=p, dklen=DK_LEN, maxmem=256 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str, pepper: bytes) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(dk)
        if not (2**10 <= int(n) <= 2**20 and 1 <= int(r) <= 32 and 1 <= int(p) <= 16):      # un hash manipulado no puede pedir memoria sin límite
            return False
        with _slots:
            got = hashlib.scrypt(_prehash(password, pepper), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                 dklen=len(expected), maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def needs_rehash(stored: str) -> bool:
    try:
        _, n, r, p, _, _ = stored.split("$")
        return (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True


_dummy: str | None = None


def dummy_hash(pepper: bytes) -> str:
    """Hash de relleno: se verifica cuando el correo no existe para que el tiempo de respuesta no delate cuentas."""
    global _dummy
    if _dummy is None:
        _dummy = hash_password(secrets.token_urlsafe(16), pepper)
    return _dummy


# ---------------------------------------------------------------- política
_COMMON = {
    "password", "contrasena", "contraseña", "123456", "12345678", "123456789", "1234567890", "qwerty", "qwertyuiop", "abc123",
    "111111", "000000", "iloveyou", "teamo", "tequiero", "admin", "administrador", "welcome", "bienvenido", "letmein", "monkey",
    "dragon", "football", "futbol", "master", "login", "princess", "sunshine", "superman", "batman", "chile", "santiago",
    "colocolo", "universidad", "passw0rd", "p4ssw0rd", "changeme", "cambiame", "secret", "secreto", "clave", "claves",
    "mipassword", "miclave", "cloudcost", "finops", "aws", "azure", "google", "cloud", "verano", "invierno", "primavera",
    "otono", "navidad", "familia", "amor", "dinero", "trustno1", "freedom", "whatever", "starwars", "pokemon", "hola",
    "holamundo", "pass", "test", "testing", "demo", "usuario", "user",
}
_LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890", "abcdefghijklmnopqrstuvwxyz")


def _has_sequence(s: str, run: int = 5) -> bool:
    s = s.lower()
    for row in _KEYBOARD_ROWS:
        for seq in (row, row[::-1]):
            for i in range(len(seq) - run + 1):
                if seq[i:i + run] in s:
                    return True
    return False


def policy_errors(password: str, *, email: str = "", name: str = "") -> list[str]:
    """Reglas (en español, para mostrar al usuario). Lista vacía = contraseña aceptable."""
    errors: list[str] = []
    if len(password) < MIN_LENGTH:
        errors.append(f"Usa al menos {MIN_LENGTH} caracteres.")
    if len(password) > MAX_LENGTH:
        errors.append(f"Usa como máximo {MAX_LENGTH} caracteres.")
        return errors
    low = unicodedata.normalize("NFKD", password).encode("ascii", "ignore").decode().lower()
    stem = re.sub(r"[\d\W_]+$", "", low)                       # sin el sufijo numérico/símbolos ("password2024!" -> "password")
    plain = re.sub(r"[^a-z0-9]", "", low.translate(_LEET))
    plain_stem = re.sub(r"[^a-z0-9]", "", stem.translate(_LEET))
    core = re.sub(r"[^a-z0-9]", "", stem)
    if low in _COMMON or plain in _COMMON or core in _COMMON or plain_stem in _COMMON:
        errors.append("Es una contraseña muy común. Elige una frase o combinación propia.")
    if len(set(password)) < 6:
        errors.append("Usa más variedad de caracteres.")
    if re.search(r"(.)\1{3,}", password):
        errors.append("No repitas el mismo carácter 4 veces seguidas.")
    if _has_sequence(password):
        errors.append("Evita secuencias como 12345 o qwerty.")
    personal = {t for t in re.split(r"[^a-z0-9]+", f"{email.split('@')[0]} {name}".lower()) if len(t) >= 4}
    if any(t in low for t in personal):
        errors.append("No uses tu nombre ni tu correo dentro de la contraseña.")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3 and len(password) < 20:
        errors.append("Combina al menos 3 tipos: minúsculas, mayúsculas, números o símbolos (o usa una frase de 20+ caracteres).")
    return errors


def pwned_count(password: str, *, timeout: float = 3.0) -> int:
    """Veces que la contraseña aparece en filtraciones (API de rango por k-anonimato: solo se envían 5 caracteres del SHA-1).

    Falla abierto: si el servicio no responde devuelve 0 para no bloquear el registro.
    """
    sha1 = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()  # noqa: S324 (lo exige la API)
    try:
        req = urllib.request.Request(f"https://api.pwnedpasswords.com/range/{sha1[:5]}", headers={"Add-Padding": "true", "User-Agent": "cloudcost"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija https)
            for line in r.read().decode().splitlines():
                suffix, _, count = line.partition(":")
                if suffix == sha1[5:]:
                    return int(count.strip() or 0)
    except Exception:
        return 0
    return 0
