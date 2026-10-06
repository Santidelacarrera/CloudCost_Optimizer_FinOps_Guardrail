"""TOTP (RFC 6238, HMAC-SHA1, 30 s, 6 dígitos) compatible con Google Authenticator, Microsoft Authenticator, Authy y 1Password."""
from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret_b32: str, step: int, digits: int = 6) -> str:
    h = hmac.new(_key(secret_b32), struct.pack(">Q", step), "sha1").digest()
    o = h[-1] & 0x0F
    n = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(n).zfill(digits)


def current_step(now: float | None = None, period: int = 30) -> int:
    return int((time.time() if now is None else now) // period)


def verify(secret_b32: str, code: str, *, last_step: int | None = None, now: float | None = None, window: int = 1,
           digits: int = 6) -> int | None:
    """Devuelve el intervalo aceptado o None. Rechaza intervalos ya usados (last_step) para impedir reutilizar un código."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != digits or not code.isdigit():
        return None
    cur = current_step(now)
    found = None
    for step in range(cur - window, cur + window + 1):          # sin cortocircuito: tiempo constante
        if hmac.compare_digest(code_at(secret_b32, step, digits), code) and (last_step is None or step > last_step):
            found = step
    return found


def otpauth_uri(issuer: str, account: str, secret_b32: str) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
