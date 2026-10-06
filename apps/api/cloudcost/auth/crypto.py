"""Tokens opacos, hashes de tokens y cifrado del secreto TOTP en reposo (AES-256-GCM con clave derivada del pepper)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SESSION_PREFIX = "ccs_"


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """sha256 del token. Basta (sin sal) porque el token tiene 256 bits de entropía: no se puede adivinar ni buscar en tablas."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_email(email: str, pepper: bytes) -> str:
    return hmac.new(pepper, b"email:" + email.strip().lower().encode(), hashlib.sha256).hexdigest()


def _aes_key(pepper: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"cloudcost/totp-secret/v1").derive(pepper)


def encrypt_secret(plain: str, pepper: bytes, bind_to: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    ct = AESGCM(_aes_key(pepper)).encrypt(nonce, plain.encode(), bind_to.encode())     # bind_to = id de la cuenta (AAD)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(blob: str, pepper: bytes, bind_to: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.urlsafe_b64decode(blob.encode())
    return AESGCM(_aes_key(pepper)).decrypt(raw[:12], raw[12:], bind_to.encode()).decode()


_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # sin caracteres ambiguos (0/o, 1/l/i)


def new_recovery_codes(n: int = 10) -> list[str]:
    return ["".join(secrets.choice(_ALPHABET) for _ in range(5)) + "-" + "".join(secrets.choice(_ALPHABET) for _ in range(5))
            for _ in range(n)]


def normalize_recovery(code: str) -> str:
    return code.strip().lower().replace(" ", "")


def hash_recovery(code: str, pepper: bytes) -> str:
    return hmac.new(pepper, b"recovery:" + normalize_recovery(code).encode(), hashlib.sha256).hexdigest()
