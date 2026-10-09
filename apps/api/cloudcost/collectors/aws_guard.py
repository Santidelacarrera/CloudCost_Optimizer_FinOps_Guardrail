"""Guardia de SOLO LECTURA para el cliente de AWS.

La promesa «la plataforma nunca escribe en tu nube» se apoya en el rol IAM del cliente, pero eso depende de que el cliente lo
haya desplegado bien. Aquí se añade un segundo cerrojo, en nuestro código: la sesión de boto3 que usa el colector rechaza —antes de
construir la solicitud— cualquier operación que no esté en esta lista cerrada. Si alguien añade mañana una llamada de escritura
(o una lectura no prevista) al colector, falla en la primera prueba y no llega a salir de la máquina.

La lista es exacta (servicio, operación), no un patrón «Describe*/List*/Get*»: ampliar permisos exige tocar este archivo, la política
IAM (Terraform y CloudFormation) y la documentación de servicios soportados; `tests/unit/test_aws_readonly_guard.py` comprueba que
las tres coinciden.
"""
from __future__ import annotations

from typing import Any

# (servicio de boto3, operación) -> acción IAM equivalente («» si no requiere permiso).
ALLOWED_OPERATIONS: dict[tuple[str, str], str] = {
    ("sts", "GetCallerIdentity"): "",                                   # no requiere permiso IAM
    ("ec2", "DescribeInstances"): "ec2:DescribeInstances",
    ("ec2", "DescribeVolumes"): "ec2:DescribeVolumes",
    ("ec2", "DescribeSnapshots"): "ec2:DescribeSnapshots",
    ("ec2", "DescribeImages"): "ec2:DescribeImages",
    ("cloudwatch", "GetMetricData"): "cloudwatch:GetMetricData",
    ("cloudwatch", "ListMetrics"): "cloudwatch:ListMetrics",
    ("cloudtrail", "LookupEvents"): "cloudtrail:LookupEvents",
    ("ce", "GetCostAndUsage"): "ce:GetCostAndUsage",
    ("ce", "GetCostAndUsageWithResources"): "ce:GetCostAndUsageWithResources",
}

GUARD_ID = "cloudcost-readonly-guard"


class ReadOnlyViolation(Exception):
    """Se intentó una operación de AWS fuera de la lista de solo lectura."""


def check_operation(service: str, operation: str) -> None:
    if (service, operation) not in ALLOWED_OPERATIONS:
        raise ReadOnlyViolation(f"Operación de AWS no permitida por la guardia de solo lectura: {service}:{operation}")


def _handler(params: dict[str, Any], model, **_: Any) -> None:       # firma de «before-parameter-build»
    check_operation(model.service_model.service_name, model.name)


def install(session) -> None:
    """Registra la guardia en una sesión de boto3 (idempotente). Debe hacerse ANTES de crear los clientes."""
    events = getattr(session, "events", None)
    if events is None:
        return
    events.register_first("before-parameter-build.*.*", _handler, unique_id=GUARD_ID)    # antes que cualquier otro manejador


def required_iam_actions() -> set[str]:
    return {a for a in ALLOWED_OPERATIONS.values() if a}
