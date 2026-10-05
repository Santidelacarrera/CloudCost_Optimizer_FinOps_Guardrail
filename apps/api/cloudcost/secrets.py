"""Resolución de secretos por referencia. En la base de datos solo se guardan referencias, nunca valores.

Formatos:  env:NOMBRE_VARIABLE   |   aws-sm:<secret-id>   (AWS Secrets Manager)
"""
from __future__ import annotations

import os


class SecretError(Exception):
    pass


class SecretResolver:
    def resolve(self, ref: str | None) -> str | None:
        if not ref:
            return None
        scheme, _, key = ref.partition(":")
        if not key:
            raise SecretError("Referencia de secreto inválida (usa env:NOMBRE o aws-sm:id)")
        if scheme == "env":
            return os.environ.get(key)
        if scheme == "aws-sm":
            import boto3  # import perezoso: solo si se usa

            try:
                return boto3.client("secretsmanager").get_secret_value(SecretId=key)["SecretString"]
            except Exception as exc:
                raise SecretError(f"No se pudo leer el secreto {key}: {type(exc).__name__}") from exc
        raise SecretError(f"Esquema de secreto no soportado: {scheme}")
