"""Onboarding de cuentas AWS con CloudFormation: ExternalId nuevo y enlace de creación rápida (quick-create).

Módulo puro (sin base de datos ni red): construye datos, no crea nada en AWS. El cliente abre el enlace en su consola,
revisa la plantilla y pulsa «Crear pila»; CloudCost nunca recibe credenciales, solo el ARN del rol resultante.
"""
from __future__ import annotations

import re
import secrets
from urllib.parse import urlencode, urlparse

CONSOLE_URL = "https://{region}.console.aws.amazon.com/cloudformation/home"
DEFAULT_STACK_NAME = "CloudCostOptimizer-ReadOnly"
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_PRINCIPAL = re.compile(r"^arn:aws(-cn|-us-gov)?:iam::\d{12}:(root|role/[\w+=,.@/-]+|user/[\w+=,.@/-]+)$")
ONBOARDING_STARTED = "ONBOARDING_STARTED"


class OnboardingError(ValueError):
    pass


def new_external_id() -> str:
    """ExternalId aleatorio (192 bits). Cumple el patrón de la plantilla: [A-Za-z0-9_-], 35 caracteres."""
    return "cc-" + secrets.token_urlsafe(24)


def validate_template_url(url: str | None) -> str:
    if not url:
        raise OnboardingError("AWS_ONBOARDING_TEMPLATE_URL no está configurada (URL https de S3 con readonly-role.yaml)")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise OnboardingError("AWS_ONBOARDING_TEMPLATE_URL debe ser https://")
    if not parsed.netloc.endswith(".amazonaws.com") and not parsed.netloc.endswith(".amazonaws.com.cn"):
        # CloudFormation solo acepta plantillas alojadas en S3.
        raise OnboardingError("CloudFormation solo admite plantillas alojadas en S3 (…amazonaws.com)")
    return url


def quick_create_url(*, template_url: str, principal_arn: str, external_id: str, region: str = "us-east-1",
                     stack_name: str = DEFAULT_STACK_NAME, enable_cost_explorer: bool = True) -> str:
    """Enlace a la consola de AWS con la pila precargada. Los parámetros viajan en el fragmento (#), no al servidor de AWS."""
    if not _REGION.match(region):
        raise OnboardingError("Región AWS inválida")
    if not _PRINCIPAL.match(principal_arn):
        raise OnboardingError("AWS_PLATFORM_PRINCIPAL_ARN debe ser el ARN concreto de un rol, usuario o cuenta")
    validate_template_url(template_url)
    query = urlencode({
        "templateURL": template_url,
        "stackName": stack_name,
        "param_TrustedPrincipalArn": principal_arn,
        "param_ExternalId": external_id,
        "param_EnableCostExplorer": "true" if enable_cost_explorer else "false",
    })
    return f"{CONSOLE_URL.format(region=region)}?region={region}#/stacks/quickcreate?{query}"


def launch_info(*, template_url: str | None, principal_arn: str | None, region: str, enable_cost_explorer: bool = True) -> dict:
    """Datos para el asistente: ExternalId nuevo (se muestra una sola vez), enlace y siguiente paso."""
    if not principal_arn:
        raise OnboardingError("AWS_PLATFORM_PRINCIPAL_ARN no está configurado")
    external_id = new_external_id()
    return {
        "external_id": external_id,
        "trusted_principal_arn": principal_arn,
        "region": region,
        "quick_create_url": quick_create_url(template_url=validate_template_url(template_url), principal_arn=principal_arn,
                                             external_id=external_id, region=region, enable_cost_explorer=enable_cost_explorer),
        "next_steps": [
            "Guarda el external_id en tu gestor de secretos (env:NOMBRE o aws-sm:id); CloudCost solo guarda la referencia.",
            "Abre quick_create_url, revisa la plantilla (solo lectura) y pulsa «Crear pila».",
            "Copia la salida RoleArn y registra la cuenta con POST /api/v1/cloud-accounts (role_arn + external_id_ref).",
        ],
    }
