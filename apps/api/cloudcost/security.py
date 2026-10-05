"""Autenticación (JWT: HS256 en dev, OIDC/JWKS en producción) y autorización RBAC."""
from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request

from .config import Settings, get_settings

ROLES = ("ADMIN", "FINOPS", "SRE", "DEVELOPER", "AUDITOR", "VIEWER")

# Matriz de permisos
SCAN = ("ADMIN", "FINOPS", "SRE")
APPROVE = ("ADMIN", "FINOPS", "SRE")
CREATE_PR = ("ADMIN", "FINOPS", "SRE")
MARK_DEPLOYED = ("ADMIN", "SRE")
VERIFY_SAVINGS = ("ADMIN", "FINOPS", "SRE")
MANAGE_CONNECTIONS = ("ADMIN",)
READ_AUDIT = ("ADMIN", "AUDITOR")
READ = ROLES


@dataclass(frozen=True)
class Principal:
    user_id: str          # claim "sub"
    email: str | None
    org_id: UUID
    role: str


@lru_cache
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True)


def mint_dev_token(settings: Settings, *, email: str, role: str, org_id: str | None = None, ttl: int = 8 * 3600) -> str:
    now = int(time.time())
    return jwt.encode({"sub": f"dev:{email}", "email": email, "role": role, "org_id": org_id or settings.dev_default_org_id,
                       "iss": "cloudcost-dev", "iat": now, "exp": now + ttl},
                      settings.jwt_secret.get_secret_value(), algorithm="HS256")


def decode_token(token: str, settings: Settings) -> Principal:
    try:
        if settings.auth_mode == "dev":
            claims = jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"],
                                issuer="cloudcost-dev", options={"require": ["exp", "sub"]})
            org_claim, role_claim = "org_id", "role"
        else:
            key = _jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=settings.oidc_audience,
                                issuer=settings.oidc_issuer, options={"require": ["exp", "sub"]})
            org_claim, role_claim = settings.oidc_org_claim, settings.oidc_role_claim
        org_id = UUID(str(claims[org_claim]))
        role = str(claims[role_claim]).upper()
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(401, "Token inválido o expirado", headers={"WWW-Authenticate": "Bearer"}) from exc
    if role not in ROLES:
        raise HTTPException(403, "Rol no reconocido")
    return Principal(user_id=str(claims["sub"]), email=claims.get("email"), org_id=org_id, role=role)


def get_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Falta el token Bearer", headers={"WWW-Authenticate": "Bearer"})
    return decode_token(token, settings)


def require(*roles: str) -> Callable[[Principal], Principal]:
    """Dependencia FastAPI: exige que el rol del token esté entre `roles`."""
    allowed = set(roles)

    def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(403, f"El rol {principal.role} no tiene permiso para esta operación")
        return principal

    return checker
