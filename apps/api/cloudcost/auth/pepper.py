"""Anillo de peppers: permite rotar el secreto del servidor sin invalidar contraseñas ni 2FA.

Cada dato protegido con el pepper guarda el **id** del pepper con el que se creó:
  * hash de contraseña   scrypt$n$r$p$sal$hash[$id]
  * secreto TOTP cifrado v2.<id>.<blob>
  * código de recuperación  <id>:<hmac>
El id `1` (LEGACY_ID) conserva el formato histórico sin etiqueta: un despliegue sin rotar produce y lee exactamente los mismos bytes
que antes. Al rotar, el pepper nuevo pasa a ser el actual (`AUTH_PEPPER` + `AUTH_PEPPER_ID`) y el anterior sigue disponible, solo para
verificar y descifrar, en `AUTH_PEPPER_PREVIOUS` (`id:secreto,id:secreto`).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Mapping

LEGACY_ID = "1"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
MIN_SECRET_LENGTH = 32


class PepperError(ValueError):
    pass


def check_id(pepper_id: str) -> str:
    if not _ID.match(pepper_id or ""):
        raise PepperError(f"Id de pepper inválido: {pepper_id!r} (usa 1-16 caracteres [A-Za-z0-9_-])")
    return pepper_id


class PepperRing:
    """Pepper actual (para crear datos nuevos) + peppers anteriores (solo para leer datos antiguos)."""

    __slots__ = ("current_id", "_keys")

    def __init__(self, current_id: str, current: bytes, previous: Mapping[str, bytes] | None = None):
        check_id(current_id)
        keys = {current_id: current}
        for pid, secret in (previous or {}).items():
            check_id(pid)
            if pid in keys:
                raise PepperError(f"El id de pepper {pid!r} está repetido")
            if secret in keys.values():
                raise PepperError("Un pepper anterior no puede ser igual al actual")
            keys[pid] = secret
        self.current_id = current_id
        self._keys = keys

    @property
    def current(self) -> bytes:
        return self._keys[self.current_id]

    @property
    def ids(self) -> tuple[str, ...]:
        """Id actual primero, luego los anteriores."""
        return (self.current_id, *(i for i in self._keys if i != self.current_id))

    def get(self, pepper_id: str) -> bytes | None:
        return self._keys.get(pepper_id)

    def is_current(self, pepper_id: str) -> bool:
        return pepper_id == self.current_id


def parse_previous(text: str | None) -> dict[str, str]:
    """`1:secretoViejo,2:otro` -> {"1": "secretoViejo", "2": "otro"}. El id y el secreto se separan en el primer `:`."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        pid, sep, secret = part.partition(":")
        pid, secret = pid.strip(), secret.strip()
        if not sep or not secret:
            raise PepperError("AUTH_PEPPER_PREVIOUS debe tener el formato id:secreto[,id:secreto]")
        check_id(pid)
        if pid in out:
            raise PepperError(f"El id de pepper {pid!r} está repetido en AUTH_PEPPER_PREVIOUS")
        out[pid] = secret
    return out


@lru_cache(maxsize=8)
def build_ring(current_secret: str, current_id: str = LEGACY_ID, previous: str | None = None) -> PepperRing:
    prev = {pid: s.encode() for pid, s in parse_previous(previous).items()}
    return PepperRing(current_id, current_secret.encode(), prev)


def validate_for_production(current_secret: str, previous: str | None) -> list[str]:
    """Problemas de los peppers anteriores en producción (el actual ya lo valida la configuración)."""
    problems = []
    for pid, secret in parse_previous(previous).items():
        if len(secret) < MIN_SECRET_LENGTH or secret.startswith("dev-only"):
            problems.append(f"El pepper anterior {pid!r} debe ser un secreto de al menos {MIN_SECRET_LENGTH} caracteres")
    return problems
