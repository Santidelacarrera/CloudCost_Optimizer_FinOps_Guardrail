"""Tokens opacos, hashes de tokens y cifrado del secreto TOTP en reposo (AES-256-GCM con clave derivada del pepper)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from .pepper import LEGACY_ID, PepperError, PepperRing, check_id

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


def encrypt_secret(plain: str, pepper: bytes, bind_to: str, pepper_id: str = LEGACY_ID) -> str:
    """AES-256-GCM. El blob de un pepper distinto del histórico lleva el prefijo `v2.<id>.` para poder descifrarlo tras una rotación."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    check_id(pepper_id)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_aes_key(pepper)).encrypt(nonce, plain.encode(), bind_to.encode())     # bind_to = id de la cuenta (AAD)
    blob = base64.urlsafe_b64encode(nonce + ct).decode()
    return blob if pepper_id == LEGACY_ID else f"v2.{pepper_id}.{blob}"


def _split_secret(blob: str) -> tuple[str, str]:
    if blob.startswith("v2."):
        _, pepper_id, body = blob.split(".", 2)
        return check_id(pepper_id), body
    return LEGACY_ID, blob


def secret_pepper_id(blob: str) -> str:
    return _split_secret(blob)[0]


def decrypt_secret(blob: str, pepper: bytes | PepperRing, bind_to: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pepper_id, body = _split_secret(blob)
    if isinstance(pepper, PepperRing):
        key = pepper.get(pepper_id)
        if key is None:
            raise PepperError(f"El pepper {pepper_id!r} con el que se cifró este secreto ya no está en AUTH_PEPPER / AUTH_PEPPER_PREVIOUS")
        pepper = key
    raw = base64.urlsafe_b64decode(body.encode())
    return AESGCM(_aes_key(pepper)).decrypt(raw[:12], raw[12:], bind_to.encode()).decode()


def reencrypt_secret(blob: str, ring: PepperRing, bind_to: str) -> str:
    """Mismo secreto, cifrado con el pepper actual."""
    return encrypt_secret(decrypt_secret(blob, ring, bind_to), ring.current, bind_to, ring.current_id)


_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # sin caracteres ambiguos (0/o, 1/l/i)


def new_recovery_codes(n: int = 10) -> list[str]:
    return ["".join(secrets.choice(_ALPHABET) for _ in range(5)) + "-" + "".join(secrets.choice(_ALPHABET) for _ in range(5))
            for _ in range(n)]


def normalize_recovery(code: str) -> str:
    return code.strip().lower().replace(" ", "")


def hash_recovery(code: str, pepper: bytes, pepper_id: str = LEGACY_ID) -> str:
    """HMAC del código. Con un pepper distinto del histórico se guarda como `<id>:<hmac>`."""
    check_id(pepper_id)
    digest = hmac.new(pepper, b"recovery:" + normalize_recovery(code).encode(), hashlib.sha256).hexdigest()
    return digest if pepper_id == LEGACY_ID else f"{pepper_id}:{digest}"


def recovery_candidates(code: str, ring: PepperRing) -> list[str]:
    """Todas las formas con las que puede estar guardado el código (una por pepper del anillo)."""
    return [hash_recovery(code, ring.get(pid), pid) for pid in ring.ids]


def recovery_pepper_id(code_hash: str) -> str:
    return code_hash.split(":", 1)[0] if ":" in code_hash else LEGACY_ID
