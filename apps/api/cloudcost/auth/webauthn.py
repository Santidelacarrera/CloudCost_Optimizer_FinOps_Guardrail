"""WebAuthn / passkeys (nivel 2 de la especificación del W3C) sin dependencias nuevas: solo `cryptography`.

Lógica pura (sin base de datos ni FastAPI) para poder probarla entera con un autenticador de software:
  opciones de registro/autenticación → verificación de la respuesta del navegador.

Qué se verifica (cada punto tiene su prueba):
  * `clientDataJSON`: tipo (`webauthn.create`/`webauthn.get`), challenge idéntico al emitido, origen EXACTO permitido, sin `crossOrigin`.
  * `authenticatorData`: hash del RP ID, bandera UP (presencia) y UV (verificación del usuario: PIN/biometría) obligatorias,
    coherencia de las banderas de respaldo (BS ⇒ BE) y contador de firmas (si avanza, debe crecer: detecta clonación).
  * Firma sobre `authData || sha256(clientDataJSON)` con ES256, EdDSA o RS256; la clave se guarda como SubjectPublicKeyInfo DER.
  * Registro: el `credentialId` de la respuesta coincide con el de los datos del autenticador y su tamaño es razonable.

Qué NO se hace: no se valida la atestación (se pide `attestation: "none"`). No identificamos el fabricante de la llave; sirve para
reconocer «esta es una llave de esta cuenta», no para restringir modelos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key

ALG_ES256, ALG_EDDSA, ALG_RS256 = -7, -8, -257
SUPPORTED_ALGS = (ALG_ES256, ALG_EDDSA, ALG_RS256)
FLAG_UP, FLAG_UV, FLAG_BE, FLAG_BS, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
CHALLENGE_BYTES = 32
CHALLENGE_TTL = 300
MAX_CREDENTIAL_ID = 1023
MAX_CBOR_DEPTH = 8
MAX_PAYLOAD = 16384


class WebAuthnError(Exception):
    """`code` es estable (la interfaz lo traduce); `message` es seguro de mostrar."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    if not isinstance(text, str) or len(text) > MAX_PAYLOAD * 2 or not text.replace("-", "").replace("_", "").isalnum():
        raise WebAuthnError("invalid_response", "La respuesta de la llave de acceso no es válida.")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_challenge() -> bytes:
    return secrets.token_bytes(CHALLENGE_BYTES)


# ------------------------------------------------------------------------------------------------------------ CBOR mínimo
def cbor_decode(data: bytes, offset: int = 0, _depth: int = 0) -> tuple[Any, int]:
    """Decodificador CBOR acotado (RFC 8949) para lo que usa WebAuthn: enteros, bytes, texto, listas, mapas, booleanos y nulo.
    Rechaza longitudes indefinidas, anidamiento excesivo y tamaños mayores que los datos."""
    if _depth > MAX_CBOR_DEPTH:
        raise WebAuthnError("invalid_response", "Datos CBOR demasiado anidados.")
    try:
        head = data[offset]
    except IndexError as exc:
        raise WebAuthnError("invalid_response", "Datos CBOR truncados.") from exc
    major, info = head >> 5, head & 0x1F
    offset += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if offset + size > len(data):
            raise WebAuthnError("invalid_response", "Datos CBOR truncados.")
        arg = int.from_bytes(data[offset:offset + size], "big")
        offset += size
    else:
        raise WebAuthnError("invalid_response", "CBOR de longitud indefinida no soportado.")
    if major == 0:
        return arg, offset
    if major == 1:
        return -1 - arg, offset
    if major in (2, 3):
        if arg > len(data) - offset:
            raise WebAuthnError("invalid_response", "Datos CBOR truncados.")
        chunk = data[offset:offset + arg]
        try:
            return (bytes(chunk) if major == 2 else chunk.decode("utf-8")), offset + arg
        except UnicodeDecodeError as exc:
            raise WebAuthnError("invalid_response", "Texto CBOR inválido.") from exc
    if major == 4:
        if arg > len(data) - offset:
            raise WebAuthnError("invalid_response", "Datos CBOR truncados.")
        items = []
        for _ in range(arg):
            item, offset = cbor_decode(data, offset, _depth + 1)
            items.append(item)
        return items, offset
    if major == 5:
        if arg > len(data) - offset:
            raise WebAuthnError("invalid_response", "Datos CBOR truncados.")
        out: dict[Any, Any] = {}
        for _ in range(arg):
            key, offset = cbor_decode(data, offset, _depth + 1)
            if not isinstance(key, (int, str, bytes)) or key in out:
                raise WebAuthnError("invalid_response", "Clave CBOR inválida o repetida.")
            out[key], offset = cbor_decode(data, offset, _depth + 1)
        return out, offset
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], offset
    raise WebAuthnError("invalid_response", "Tipo CBOR no soportado.")


# ------------------------------------------------------------------------------------------------------------ opciones
def creation_options(*, rp_id: str, rp_name: str, user_id: bytes, user_name: str, display_name: str, challenge: bytes,
                     exclude: list[bytes]) -> dict:
    return {
        "rp": {"id": rp_id, "name": rp_name},
        "user": {"id": b64u(user_id), "name": user_name, "displayName": display_name},
        "challenge": b64u(challenge),
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in SUPPORTED_ALGS],
        "timeout": 120000,
        "attestation": "none",
        "excludeCredentials": [{"type": "public-key", "id": b64u(c)} for c in exclude],
        "authenticatorSelection": {"residentKey": "preferred", "userVerification": "required"},
    }


def request_options(*, rp_id: str, challenge: bytes, allow: list[tuple[bytes, list[str]]]) -> dict:
    return {
        "rpId": rp_id, "challenge": b64u(challenge), "timeout": 120000, "userVerification": "required",
        "allowCredentials": [{"type": "public-key", "id": b64u(cid), **({"transports": t} if t else {})} for cid, t in allow],
    }


# ------------------------------------------------------------------------------------------------------------ verificación
@dataclass(frozen=True)
class ClientData:
    type: str
    challenge: str
    origin: str


def parse_client_data(raw: bytes) -> ClientData:
    if len(raw) > MAX_PAYLOAD:
        raise WebAuthnError("invalid_response", "La respuesta de la llave de acceso es demasiado grande.")
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("no es un objeto")
        cd = ClientData(str(data["type"]), str(data["challenge"]), str(data["origin"]))
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        raise WebAuthnError("invalid_response", "La respuesta de la llave de acceso no es válida.") from exc
    if data.get("crossOrigin") or "topOrigin" in data:                    # uso desde un iframe de otro sitio
        raise WebAuthnError("invalid_origin", "La llave de acceso se usó desde un contexto no permitido.")
    return cd


def client_challenge(client_data_json: bytes) -> str:
    """Challenge declarado por el navegador (para localizar el challenge emitido por el servidor)."""
    return parse_client_data(client_data_json).challenge


def _check_client_data(raw: bytes, *, expected_type: str, challenge: bytes, origins: tuple[str, ...]) -> None:
    cd = parse_client_data(raw)
    if cd.type != expected_type:
        raise WebAuthnError("invalid_response", "La respuesta no corresponde a esta operación.")
    if not hmac.compare_digest(cd.challenge.encode(), b64u(challenge).encode()):
        raise WebAuthnError("invalid_challenge", "La verificación venció o ya se usó. Inténtalo de nuevo.")
    if cd.origin not in origins:
        raise WebAuthnError("invalid_origin", "La llave de acceso no se creó para este sitio.")


@dataclass(frozen=True)
class AuthData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    aaguid: bytes | None = None
    credential_id: bytes | None = None
    cose_key: dict | None = None

    @property
    def backup_eligible(self) -> bool:
        return bool(self.flags & FLAG_BE)

    @property
    def backup_state(self) -> bool:
        return bool(self.flags & FLAG_BS)


def parse_auth_data(raw: bytes) -> AuthData:
    if len(raw) < 37 or len(raw) > MAX_PAYLOAD:
        raise WebAuthnError("invalid_response", "Datos del autenticador inválidos.")
    rp_hash, flags, count = raw[:32], raw[32], struct.unpack(">I", raw[33:37])[0]
    if flags & FLAG_BS and not flags & FLAG_BE:
        raise WebAuthnError("invalid_response", "Banderas de respaldo incoherentes.")
    if not flags & FLAG_AT:
        return AuthData(rp_hash, flags, count)
    if len(raw) < 55:
        raise WebAuthnError("invalid_response", "Datos del autenticador truncados.")
    aaguid, cred_len = raw[37:53], struct.unpack(">H", raw[53:55])[0]
    if cred_len == 0 or cred_len > MAX_CREDENTIAL_ID or 55 + cred_len > len(raw):
        raise WebAuthnError("invalid_response", "Identificador de credencial inválido.")
    cred_id = raw[55:55 + cred_len]
    key, _ = cbor_decode(raw, 55 + cred_len)
    if not isinstance(key, dict):
        raise WebAuthnError("invalid_response", "Clave pública inválida.")
    return AuthData(rp_hash, flags, count, aaguid, cred_id, key)


def _check_auth_data(ad: AuthData, rp_id: str) -> None:
    if not hmac.compare_digest(ad.rp_id_hash, hashlib.sha256(rp_id.encode()).digest()):
        raise WebAuthnError("invalid_rp", "La llave de acceso no se creó para este sitio.")
    if not ad.flags & FLAG_UP:
        raise WebAuthnError("user_not_present", "No se confirmó la presencia del usuario.")
    if not ad.flags & FLAG_UV:
        raise WebAuthnError("user_not_verified", "La llave debe verificar tu identidad (PIN o biometría).")


def cose_to_public_key(cose: dict) -> tuple[int, bytes]:
    """Convierte una clave COSE a (alg, SubjectPublicKeyInfo DER). Solo ES256, EdDSA y RS256, con parámetros exactos."""
    try:
        kty, alg = cose[1], cose[3]
        if alg == ALG_ES256 and kty == 2 and cose[-1] == 1:
            x, y = cose[-2], cose[-3]
            if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
                raise ValueError("coordenadas")
            key = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
        elif alg == ALG_EDDSA and kty == 1 and cose[-1] == 6:
            x = cose[-2]
            if not (isinstance(x, bytes) and len(x) == 32):
                raise ValueError("clave")
            key = ed25519.Ed25519PublicKey.from_public_bytes(x)
        elif alg == ALG_RS256 and kty == 3:
            n, e = cose[-1], cose[-2]
            if not (isinstance(n, bytes) and isinstance(e, bytes)) or len(n) < 256:        # RSA ≥ 2048 bits
                raise ValueError("módulo")
            key = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
        else:
            raise ValueError("algoritmo")
    except (KeyError, ValueError, TypeError) as exc:
        raise WebAuthnError("unsupported_key", "El tipo de llave no está soportado (usa ES256, EdDSA o RS256).") from exc
    return alg, key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


@dataclass(frozen=True)
class NewCredential:
    credential_id: bytes
    public_key: bytes
    alg: int
    sign_count: int
    aaguid: bytes
    backup_eligible: bool
    backup_state: bool


def verify_registration(*, client_data_json: bytes, attestation_object: bytes, credential_id_b64: str, challenge: bytes, origins: tuple[str, ...],
                        rp_id: str) -> NewCredential:
    _check_client_data(client_data_json, expected_type="webauthn.create", challenge=challenge, origins=origins)
    att, end = cbor_decode(attestation_object)
    if end != len(attestation_object) or not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise WebAuthnError("invalid_response", "Objeto de atestación inválido.")
    ad = parse_auth_data(att["authData"])
    _check_auth_data(ad, rp_id)
    if ad.credential_id is None or ad.cose_key is None:
        raise WebAuthnError("invalid_response", "La respuesta no trae la credencial.")
    if unb64u(credential_id_b64) != ad.credential_id:
        raise WebAuthnError("invalid_response", "El identificador de la credencial no coincide.")
    alg, spki = cose_to_public_key(ad.cose_key)
    return NewCredential(ad.credential_id, spki, alg, ad.sign_count, ad.aaguid or b"\0" * 16, ad.backup_eligible, ad.backup_state)


@dataclass(frozen=True)
class Assertion:
    sign_count: int
    backup_state: bool
    user_handle: bytes | None


def _verify_signature(alg: int, spki: bytes, signature: bytes, data: bytes) -> None:
    try:
        key = load_der_public_key(spki)
        if alg == ALG_ES256 and isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, data, ec.ECDSA(SHA256()))
        elif alg == ALG_EDDSA and isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, data)
        elif alg == ALG_RS256 and isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, data, padding.PKCS1v15(), SHA256())
        else:
            raise WebAuthnError("unsupported_key", "La llave guardada no coincide con su algoritmo.")
    except InvalidSignature as exc:
        raise WebAuthnError("invalid_signature", "La firma de la llave de acceso no es válida.", 401) from exc
    except (ValueError, TypeError) as exc:
        raise WebAuthnError("invalid_signature", "La firma de la llave de acceso no es válida.", 401) from exc


def verify_assertion(*, client_data_json: bytes, authenticator_data: bytes, signature: bytes, user_handle: bytes | None, challenge: bytes,
                     origins: tuple[str, ...], rp_id: str, public_key: bytes, alg: int, stored_sign_count: int) -> Assertion:
    _check_client_data(client_data_json, expected_type="webauthn.get", challenge=challenge, origins=origins)
    ad = parse_auth_data(authenticator_data)
    _check_auth_data(ad, rp_id)
    _verify_signature(alg, public_key, signature, authenticator_data + hashlib.sha256(client_data_json).digest())
    # Contador: los autenticadores sincronizados (passkeys en la nube) devuelven siempre 0; si alguno de los dos es distinto de 0 debe crecer.
    if (ad.sign_count or stored_sign_count) and ad.sign_count <= stored_sign_count:
        raise WebAuthnError("counter_regression", "La llave de acceso parece clonada. Por seguridad no se aceptó.", 401)
    return Assertion(ad.sign_count, ad.backup_state, user_handle)
