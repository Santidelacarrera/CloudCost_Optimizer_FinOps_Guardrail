"""Autenticador WebAuthn de software para las pruebas: hace lo que haría el navegador + una llave (crear y firmar), con opciones para
producir respuestas manipuladas. Incluye un codificador CBOR mínimo (el servidor solo trae el decodificador)."""
from __future__ import annotations

import hashlib
import json
import os
import struct

from cloudcost.auth.webauthn import ALG_EDDSA, ALG_ES256, ALG_RS256, b64u, unb64u
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256


def cbor(obj) -> bytes:
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError(n)

    if obj is True:
        return b"\xf5"
    if obj is False:
        return b"\xf4"
    if obj is None:
        return b"\xf6"
    if isinstance(obj, int):
        return head(0, obj) if obj >= 0 else head(1, -1 - obj)
    if isinstance(obj, bytes):
        return head(2, len(obj)) + obj
    if isinstance(obj, str):
        raw = obj.encode()
        return head(3, len(raw)) + raw
    if isinstance(obj, list):
        return head(4, len(obj)) + b"".join(cbor(i) for i in obj)
    if isinstance(obj, dict):
        return head(5, len(obj)) + b"".join(cbor(k) + cbor(v) for k, v in obj.items())
    raise TypeError(type(obj))


class Authenticator:
    def __init__(self, alg: int = ALG_ES256, *, rp_id: str = "app.acme.com", origin: str = "https://app.acme.com", synced: bool = False):
        self.alg, self.rp_id, self.origin, self.synced = alg, rp_id, origin, synced
        if alg == ALG_ES256:
            self.key = ec.generate_private_key(ec.SECP256R1())
        elif alg == ALG_EDDSA:
            self.key = ed25519.Ed25519PrivateKey.generate()
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.credential_id = os.urandom(32)
        self.count = 0
        self.user_handle: bytes | None = None

    # --- clave pública en formato COSE
    def cose(self) -> dict:
        pub = self.key.public_key()
        if self.alg == ALG_ES256:
            n = pub.public_numbers()
            return {1: 2, 3: ALG_ES256, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        if self.alg == ALG_EDDSA:
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            return {1: 1, 3: ALG_EDDSA, -1: 6, -2: pub.public_bytes(Encoding.Raw, PublicFormat.Raw)}
        n = pub.public_numbers()
        return {1: 3, 3: ALG_RS256, -1: n.n.to_bytes(256, "big"), -2: n.e.to_bytes(3, "big")}

    def _flags(self, base: int, uv: bool, up: bool) -> int:
        return base | (0x01 if up else 0) | (0x04 if uv else 0) | ((0x08 | 0x10) if self.synced else 0)

    def _client_data(self, typ: str, challenge: str, origin: str | None, extra: dict | None) -> bytes:
        return json.dumps({"type": typ, "challenge": challenge, "origin": origin or self.origin, **(extra or {})}).encode()

    def create(self, options: dict, *, origin: str | None = None, rp_id: str | None = None, uv: bool = True, up: bool = True,
               extra_client: dict | None = None, type_: str = "webauthn.create", challenge: str | None = None, id_override: bytes | None = None) -> dict:
        rp_hash = hashlib.sha256((rp_id or options["rp"]["id"]).encode()).digest()
        self.user_handle = unb64u(options["user"]["id"])
        auth = rp_hash + bytes([self._flags(0x40, uv, up)]) + struct.pack(">I", self.count) + b"\0" * 16 + struct.pack(">H", len(self.credential_id)) \
            + self.credential_id + cbor(self.cose())
        att = cbor({"fmt": "none", "attStmt": {}, "authData": auth})
        cd = self._client_data(type_, challenge or options["challenge"], origin, extra_client)
        cid = id_override or self.credential_id
        return {"id": b64u(cid), "rawId": b64u(cid), "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(att), "transports": ["internal"]}}

    def get(self, options: dict, *, origin: str | None = None, rp_id: str | None = None, uv: bool = True, up: bool = True,
            extra_client: dict | None = None, count: int | None = None, challenge: str | None = None, user_handle: bytes | None = ...) -> dict:
        if count is None:
            self.count = 0 if self.synced else self.count + 1
        else:
            self.count = count
        rp_hash = hashlib.sha256((rp_id or options["rpId"]).encode()).digest()
        auth = rp_hash + bytes([self._flags(0, uv, up)]) + struct.pack(">I", self.count)
        cd = self._client_data("webauthn.get", challenge or options["challenge"], origin, extra_client)
        data = auth + hashlib.sha256(cd).digest()
        if self.alg == ALG_ES256:
            sig = self.key.sign(data, ec.ECDSA(SHA256()))
        elif self.alg == ALG_EDDSA:
            sig = self.key.sign(data)
        else:
            sig = self.key.sign(data, padding.PKCS1v15(), SHA256())
        uh = self.user_handle if user_handle is ... else user_handle
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth), "signature": b64u(sig),
                             "userHandle": b64u(uh) if uh else None}}
