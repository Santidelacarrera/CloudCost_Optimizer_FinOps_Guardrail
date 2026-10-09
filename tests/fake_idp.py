"""IdP OIDC simulado en memoria para las pruebas de SSO: discovery, JWKS y token endpoint, con una clave RSA generada al vuelo."""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import jwt
from cloudcost.auth import oidc
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = "11111111-2222-3333-4444-555555555555"
ENTRA_ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
OKTA_ISSUER = "https://acme.okta.com/oauth2/default"
CLIENT_ID = "cloudcost-client"


class _Resp:
    def __init__(self, status: int, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeIdP:
    """Servidor OIDC mínimo en memoria. Cuenta las llamadas para comprobar cachés y guarda la última petición de canje."""

    def __init__(self, issuer: str, *, kid: str = "k1", host: str | None = None):
        self.issuer, self.kid = issuer, kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.host = host or urlparse(issuer).hostname
        self.calls: list[str] = []
        self.token_requests: list[dict] = []
        self.id_token: str | None = None
        self.token_status = 200
        self.metadata_overrides: dict = {}
        self.extra_keys: list[dict] = []

    # --- publicación de claves y metadatos
    def jwk(self, kid: str | None = None) -> dict:
        data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**data, "kid": kid or self.kid, "use": "sig", "alg": "RS256"}

    def metadata(self) -> dict:
        base = f"https://{self.host}"
        return {"issuer": self.issuer, "authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/keys", **self.metadata_overrides}

    # --- http falso (misma interfaz que `requests` que usa el módulo)
    def get(self, url: str, **_):
        self.calls.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return _Resp(200, self.metadata())
        if url.endswith("/keys"):
            return _Resp(200, {"keys": [self.jwk(), *self.extra_keys]})
        return _Resp(404, {})

    def post(self, url: str, data=None, **_):
        self.calls.append(url)
        self.token_requests.append(dict(data or {}))
        if self.token_status != 200:
            return _Resp(self.token_status, {"error": "invalid_grant", "error_description": "usuario ana@acme.com no existe"})
        return _Resp(200, {"id_token": self.id_token, "access_token": "no-se-usa"})

    # --- emisión de tokens
    def sign(self, claims: dict, *, alg: str = "RS256", key=None, kid: str | None = None) -> str:
        return jwt.encode(claims, key or self.key, algorithm=alg, headers={"kid": kid or self.kid})

    def claims(self, cfg: oidc.SsoConfig, nonce: str, **over) -> dict:
        now = int(time.time())
        base = {"iss": self.issuer, "aud": cfg.client_id, "sub": "user-123", "iat": now, "exp": now + 300, "nonce": nonce,
                "email": "ana@acme.com", "name": "Ana Pérez"}
        if cfg.provider == "entra":
            base["tid"] = TENANT
        return {**base, **over}
