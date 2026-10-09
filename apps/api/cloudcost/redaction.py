"""Redacción de secretos: nada que parezca una credencial debe llegar a un log, a un mensaje de error persistido, a la auditoría ni a una respuesta.

Dos mecanismos que se complementan:

  · VALORES CONOCIDOS: cada secreto que el sistema resuelve (`secrets.SecretResolver`: ExternalId, tokens de Git, claves de nube) se
    registra aquí; a partir de ese momento, cualquier texto que lo contenga se enmascara aunque no «parezca» un secreto.
  · FORMAS RECONOCIBLES: cabeceras `Authorization`, tokens de sesión (`ccs_…`), de GitHub/GitLab, claves de acceso de AWS, JWT, URL con
    credenciales y pares `clave=valor` con nombres sensibles.

Es defensa en profundidad: el código no debería registrar secretos; si alguno lo hiciera por descuido (o un servidor remoto los repitiera
en un mensaje de error), esta capa evita que se queden grabados. No sustituye a no escribirlos.
"""
from __future__ import annotations

import re
import threading
from typing import Any

MASK = "[REDACTED]"
MIN_SECRET_LENGTH = 6                       # valores más cortos darían falsos positivos al enmascarar texto normal
MAX_KNOWN = 512

_known: dict[str, None] = {}                # conjunto ordenado (el más antiguo sale primero al llenarse)
_lock = threading.Lock()

# Nombres de clave cuyo VALOR nunca debe mostrarse. `*_ref` (referencias a secretos, no secretos) queda exento.
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|authorization|auth_header|cookie|credential|private[_-]?key|pepper|"
    r"external[_-]?id|signature|session|otp|totp|recovery|bearer)", re.IGNORECASE)
_SAFE_KEY = re.compile(r"(_ref$|_hash$|_digest$|^evidence_hash$|^token_ref$|^session_id$|^pending_token$)", re.IGNORECASE)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-hub-signature(?:-256)?|x-gitlab-token|private-token)\b(\s*[:=]\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s,;\"']+"),
     r"\1\2" + MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + MASK),
    (re.compile(r"\bccs_[A-Za-z0-9_-]{16,}"), MASK),                                         # token de sesión propio
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), MASK),                      # GitHub
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), MASK),                                       # GitLab
    (re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b"), MASK),                          # claves de acceso de AWS
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),      # JWT
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})"), MASK),              # claves de API de LLM
    (re.compile(r"(?i)(://)([^/\s:@]+):([^/\s@]+)@"), r"\1\2:" + MASK + "@"),                  # credenciales dentro de una URL
    (re.compile(r"(?i)\b((?:[a-z_]*(?:password|passwd|secret|token|api[_-]?key|external[_-]?id|session)[a-z_]*))(['\"]?\s*[=:]\s*)(['\"]?)[^\s&,;'\"]{6,}\3"),
     r"\1\2\3" + MASK + r"\3"),
    (re.compile(r"(?i)([?&](?:token|invite|code|key|secret|signature|access_token|id_token|state)=)[^&\s]{6,}"), r"\1" + MASK),
)


def register(value: str | None) -> None:
    """Registra un valor secreto resuelto para enmascararlo en adelante. Ignora vacíos y valores demasiado cortos."""
    if not value or len(value) < MIN_SECRET_LENGTH:
        return
    with _lock:
        if value in _known:
            return
        if len(_known) >= MAX_KNOWN:
            _known.pop(next(iter(_known)))
        _known[value] = None


def forget_all() -> None:
    """Solo para pruebas."""
    with _lock:
        _known.clear()


def redact_text(text: str | None) -> str:
    if not text:
        return text or ""
    out = str(text)
    with _lock:
        known = sorted(_known, key=len, reverse=True)          # primero los largos: un secreto puede contener a otro
    for value in known:
        if value in out:
            out = out.replace(value, MASK)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def is_sensitive_key(key: Any) -> bool:
    k = str(key)
    return bool(_SENSITIVE_KEY.search(k)) and not _SAFE_KEY.search(k)


def redact_obj(value: Any, *, _depth: int = 0) -> Any:
    """Copia de `value` con las claves sensibles enmascaradas y los textos redactados. Acota la profundidad para no recorrer ciclos."""
    if _depth > 12:
        return MASK
    if isinstance(value, dict):
        return {k: (MASK if is_sensitive_key(k) and isinstance(v, str) and v else redact_obj(v, _depth=_depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
