"""Resolución de secretos por referencia. En la base de datos solo se guardan referencias, nunca valores.

Formatos:  env:CC_SECRET_NOMBRE   |   aws-sm:cloudcost/<org_id>/<nombre>   (AWS Secrets Manager)

Seguridad: la referencia la escribe un usuario de la organización, así que NO puede apuntar a cualquier variable o secreto del servidor
(`env:AUTH_PEPPER`, `env:DATABASE_URL`, el secreto de otra organización…): el valor resuelto se envía a terceros (ExternalId de STS,
cabecera de autorización del proveedor Git) y de ahí podría leerse. Por eso solo se resuelven:
  · variables `CC_SECRET_*` (las crea el operador a propósito; pensado para despliegues de una sola organización), y
  · secretos de AWS Secrets Manager bajo `cloudcost/<org_id>/` (el espacio de nombres de cada organización).
"""
from __future__ import annotations

import os
import re
from uuid import UUID

from . import redaction

ENV_PREFIX = "CC_SECRET_"
SM_PREFIX = "cloudcost/"
_ENV_NAME = re.compile(r"^CC_SECRET_[A-Z0-9_]{1,100}$")
_SM_NAME = re.compile(r"^cloudcost/[A-Za-z0-9_.@+=-]{1,64}(/[A-Za-z0-9_.@+=-]{1,128}){1,4}$")


class SecretError(Exception):
    pass


def check_ref(ref: str, org_id: UUID | str | None = None) -> None:
    """Valida el formato y el espacio de nombres de una referencia. Con `org_id`, un secreto de Secrets Manager debe ser de esa organización."""
    scheme, _, key = ref.partition(":")
    if scheme == "env":
        if not _ENV_NAME.fullmatch(key):
            raise SecretError(f"Las referencias env: solo pueden apuntar a variables {ENV_PREFIX}* (en mayúsculas)")
    elif scheme == "aws-sm":
        if not _SM_NAME.fullmatch(key) or ".." in key.split("/"):
            raise SecretError(f"Las referencias aws-sm: deben tener la forma {SM_PREFIX}<org_id>/<nombre>")
        if org_id is not None and not key.startswith(f"{SM_PREFIX}{str(org_id).lower()}/"):
            raise SecretError(f"El secreto debe estar bajo {SM_PREFIX}{str(org_id).lower()}/")
    else:
        raise SecretError(f"Esquema de secreto no soportado: {scheme or ref}")


class SecretResolver:
    def resolve(self, ref: str | None, org_id: UUID | str | None = None) -> str | None:
        """Devuelve el valor. Pasa siempre `org_id` (la organización dueña de la referencia) para aislar los secretos entre organizaciones."""
        if not ref:
            return None
        scheme, _, key = ref.partition(":")
        if not key:
            raise SecretError("Referencia de secreto inválida (usa env:CC_SECRET_NOMBRE o aws-sm:cloudcost/<org_id>/nombre)")
        check_ref(ref, org_id)
        if scheme == "env":
            value = os.environ.get(key)
        elif scheme == "aws-sm":
            import boto3  # import perezoso: solo si se usa

            try:
                value = boto3.client("secretsmanager").get_secret_value(SecretId=key)["SecretString"]
            except Exception as exc:
                raise SecretError(f"No se pudo leer el secreto {key}: {type(exc).__name__}") from None
        else:
            raise SecretError(f"Esquema de secreto no soportado: {scheme}")
        redaction.register(value)              # desde ahora cualquier log, error o auditoría que lo contenga lo enmascara
        return value
