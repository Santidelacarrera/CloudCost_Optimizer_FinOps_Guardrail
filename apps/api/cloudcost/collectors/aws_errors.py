"""Clasificación de errores de AWS y reintentos acotados.

Un `ClientError` cualquiera dice poco («ClientError»). Lo que importa a quien opera el sistema es QUÉ hacer:

  permission_denied    el rol no tiene el permiso IAM → corregir la política de solo lectura
  not_enabled          Cost Explorer (o el nivel de recurso) no está activado en la cuenta → activarlo en la consola de facturación
  credentials          credenciales caducadas, inválidas o ausentes / el rol no se pudo asumir
  throttled            límite de solicitudes → se reintenta con espera exponencial; si persiste, se informa
  data_unavailable     el proveedor aún no tiene datos para ese período o ya los eliminó
  network              fallo de red o de tiempo de espera
  invalid_request      la solicitud no es válida para el proveedor (error nuestro: debe verse, no ocultarse)
  other                cualquier otro

Los mensajes que se guardan o se muestran NUNCA incluyen el texto original de la excepción (puede contener ARN, ID de cuenta o
valores de cabeceras): solo el código de AWS, la clase y una indicación fija de qué hacer.
"""
from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, TypeVar

T = TypeVar("T")

PERMISSION = "permission_denied"
NOT_ENABLED = "not_enabled"
CREDENTIALS = "credentials"
THROTTLED = "throttled"
DATA_UNAVAILABLE = "data_unavailable"
NETWORK = "network"
INVALID = "invalid_request"
OTHER = "other"

_PERMISSION_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess", "AuthorizationError",
                     "OptInRequired", "SubscriptionRequiredException"}
_CREDENTIAL_CODES = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                     "AuthFailure", "UnrecognizedClientException", "RegionDisabledException", "InvalidIdentityToken"}
_THROTTLE_CODES = {"Throttling", "ThrottlingException", "ThrottledException", "RequestLimitExceeded", "TooManyRequestsException",
                   "LimitExceededException", "RequestThrottled", "SlowDown", "ProvisionedThroughputExceededException",
                   "BandwidthLimitExceeded", "EC2ThrottledException"}
_DATA_CODES = {"DataUnavailableException", "BillExpirationException", "ResourceNotFoundException", "InvalidNextTokenException",
               "RequestChangedException"}
_INVALID_CODES = {"ValidationException", "InvalidParameterValue", "InvalidParameterCombination", "MissingParameter", "InvalidParameter",
                  "InvalidRequest", "UnknownOperationException"}

_HINTS = {
    PERMISSION: "Falta un permiso IAM en el rol de solo lectura: revisa la política (infrastructure/terraform/aws-readonly-role).",
    NOT_ENABLED: "Cost Explorer no está activado en la cuenta (o falta el nivel de recurso): actívalo en Billing → Cost Explorer.",
    CREDENTIALS: "Credenciales caducadas, inválidas o rol no asumible: comprueba el rol, el ExternalId y la confianza.",
    THROTTLED: "AWS limitó las solicitudes incluso tras reintentar: repite el escaneo más tarde o reduce regiones/frecuencia.",
    DATA_UNAVAILABLE: "AWS aún no tiene (o ya eliminó) los datos de ese período: Cost Explorer retrasa hasta 24 h.",
    NETWORK: "Fallo de red o tiempo de espera hacia AWS: comprueba la salida a Internet/endpoints y reintenta.",
    INVALID: "AWS rechazó la solicitud como inválida: es un defecto de la integración, repórtalo.",
    OTHER: "Error no clasificado de AWS: revisa los registros del worker.",
}


@dataclass(frozen=True)
class AwsIssue:
    api: str                 # p. ej. «ce:GetCostAndUsage»
    kind: str
    code: str                # código de AWS o clase de la excepción
    retryable: bool
    hint: str
    attempts: int = 1
    region: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def message(self) -> str:
        where = f" [{self.region}]" if self.region else ""
        return f"{self.api}{where}: {self.kind} ({self.code}). {self.hint}"


def error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    return str((resp.get("Error") or {}).get("Message") or "") if isinstance(resp, dict) else ""


def classify(exc: BaseException) -> tuple[str, str]:
    """(tipo, código) del error. Solo se lee el texto de AWS para distinguir «no activado» de «sin permiso»; nunca se guarda."""
    code = error_code(exc)
    name = type(exc).__name__
    if code in _THROTTLE_CODES or name in {"ThrottlingException", "ThrottledException"}:
        return THROTTLED, code
    if code in _PERMISSION_CODES:
        text = _error_message(exc).lower()
        if "cost explorer" in text and ("not enabled" in text or "enable" in text):
            return NOT_ENABLED, code
        return PERMISSION, code
    if code in _CREDENTIAL_CODES or name in {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError",
                                             "TokenRetrievalError", "UnauthorizedSSOTokenError"}:
        return CREDENTIALS, code
    if code in _DATA_CODES:
        return DATA_UNAVAILABLE, code
    if code in _INVALID_CODES or name in {"ParamValidationError", "ParamValidationException"}:
        return INVALID, code
    if name in {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError", "ConnectionClosedError", "ConnectionError",
                "ProxyConnectionError", "SSLError", "HTTPClientError", "TimeoutError", "IncompleteReadError"} or isinstance(exc, OSError):
        return NETWORK, code
    return OTHER, code


ASSUME_ROLE_HINT = ("No se pudo asumir el rol: comprueba (1) que el ExternalId es el mismo que tiene el rol, (2) que la política de confianza "
                    "del rol admite a esta identidad y (3) que esta identidad puede ejecutar sts:AssumeRole sobre ese ARN.")


def issue_from(api: str, exc: BaseException, *, attempts: int = 1, region: str | None = None) -> AwsIssue:
    kind, code = classify(exc)
    hint = ASSUME_ROLE_HINT if api == "sts:AssumeRole" and kind in (PERMISSION, CREDENTIALS) else _HINTS[kind]
    return AwsIssue(api=api, kind=kind, code=code, retryable=kind in (THROTTLED, NETWORK), hint=hint,
                    attempts=attempts, region=region)


def call_with_retry(fn: Callable[[], T], *, max_attempts: int = 4, base_delay: float = 1.0, max_delay: float = 20.0,
                    sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random,
                    on_retry: Callable[[int, str], None] | None = None) -> tuple[T, int]:
    """Ejecuta `fn`; reintenta SOLO los límites de solicitudes con espera exponencial y «full jitter». Devuelve (resultado, intentos).

    Los errores de permisos, credenciales o solicitudes inválidas no se reintentan: repetirlos solo ensucia el registro de CloudTrail
    del cliente y agota cuota. Al agotar los intentos se relanza la última excepción con `attempts` anotado en `exc.cloudcost_attempts`.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except Exception as exc:
            kind, code = classify(exc)
            if kind != THROTTLED or attempt >= max_attempts:
                exc.cloudcost_attempts = attempt  # type: ignore[attr-defined]
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * (0.5 + rng() / 2)
            if on_retry:
                on_retry(attempt, code)
            sleep(delay)
