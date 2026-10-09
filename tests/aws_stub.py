"""Clientes de AWS con respuestas guionizadas pero VALIDADAS contra los modelos reales de la API (botocore Stubber).

Un doble escrito a mano acepta cualquier parámetro; este rechaza una solicitud con un parámetro inexistente, un valor fuera de
rango o una respuesta que no cumple el esquema real del servicio. Así las pruebas sin cuenta AWS siguen comprobando que el colector
habla el protocolo correcto. No sustituyen a una prueba con una cuenta real (ver scripts/aws_lab_validate.py y docs/aws-lab-validation.md).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber

ACCOUNT = "111122223333"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 8)


def client_error(code: str, message: str = "boom", op: str = "Op", status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class StubbedSession:
    """Sesión con la forma de boto3.Session: `events` real (para la guardia) y clientes reales envueltos en un Stubber.

    Los clientes se crean al primer uso (después de que el colector instale la guardia en `events`).
    """

    def __init__(self):
        self._real = boto3.Session(aws_access_key_id="AKIAIOSFODNN7EXAMPLE", aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                                   region_name="us-east-1")
        self.events = self._real.events
        self.stubbers: dict[str, Stubber] = {}
        self.clients: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []                  # (servicio, operación) realmente enviadas
        self._pending: dict[str, list[tuple[str, Any, dict | None, str | None]]] = {}

    # -- guion --------------------------------------------------------------------------------------------------------
    def respond(self, service: str, operation: str, response: dict, expected: dict | None = None) -> "StubbedSession":
        self._pending.setdefault(service, []).append((operation, response, expected, None))
        return self

    def fail(self, service: str, operation: str, code: str, message: str = "boom", expected: dict | None = None,
             status: int = 400) -> "StubbedSession":
        self._pending.setdefault(service, []).append((operation, None, expected, (code, message, status)))
        return self

    # -- interfaz de boto3.Session ----------------------------------------------------------------------------------
    def client(self, name: str, region_name: str | None = None, **_: Any):
        if name not in self.clients:
            real = self._real.client(name, region_name=region_name or "us-east-1")
            real.meta.events.register("before-call.*.*", self._record, unique_id=f"record-{name}")
            stub = Stubber(real)
            for op, resp, expected, error in self._pending.get(name, []):
                snake = _snake(op)
                if error:
                    stub.add_client_error(snake, service_error_code=error[0], service_message=error[1], http_status_code=error[2],
                                          expected_params=expected)
                else:
                    stub.add_response(snake, resp, expected)
            stub.activate()
            self.stubbers[name], self.clients[name] = stub, real
        return self.clients[name]

    def _record(self, model, **_: Any) -> None:
        self.calls.append((model.service_model.service_name, model.name))

    def assert_all_consumed(self) -> None:
        for name, stub in self.stubbers.items():
            stub.assert_no_pending_responses()
        missing = [s for s in self._pending if s not in self.stubbers]
        assert not missing, f"servicios con respuestas guionizadas que nunca se usaron: {missing}"


def _snake(op: str) -> str:
    out = ""
    for i, ch in enumerate(op):
        if ch.isupper() and i:
            out += "_"
        out += ch.lower()
    return out


# --------------------------------------------------------------------------- respuestas de ejemplo (formas reales de la API)
def instance(iid="i-0aaa", itype="m5.2xlarge", state="running", days_old=60, tags=None) -> dict:
    return {"InstanceId": iid, "InstanceType": itype, "State": {"Name": state, "Code": 16},
            "LaunchTime": NOW - timedelta(days=days_old), "Tags": [{"Key": k, "Value": v} for k, v in (tags or {"Name": "web"}).items()]}


def describe_instances(*instances: dict, next_token: str | None = None) -> dict:
    out: dict[str, Any] = {"Reservations": [{"Instances": list(instances)}] if instances else []}
    if next_token:
        out["NextToken"] = next_token
    return out


def volume(vid="vol-0aaa", size=100, state="in-use", attached="i-0aaa", vtype="gp3", days_old=200) -> dict:
    return {"VolumeId": vid, "Size": size, "State": state, "VolumeType": vtype, "CreateTime": NOW - timedelta(days=days_old),
            "Attachments": [{"InstanceId": attached, "State": "attached"}] if attached else [], "Tags": []}


def snapshot(sid="snap-0aaa", size=50, days_old=200, tags=None) -> dict:
    return {"SnapshotId": sid, "VolumeSize": size, "State": "completed", "StartTime": NOW - timedelta(days=days_old),
            "Description": "manual", "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}


def metric_data(*series: tuple[str, list[float]]) -> dict:
    return {"MetricDataResults": [{"Id": i, "Label": i, "StatusCode": "Complete", "Values": v,
                                   "Timestamps": [NOW - timedelta(hours=n) for n in range(len(v))]} for i, v in series]}


def ce_bucket(day: date, groups: list[tuple[list[str], float]], *, metric="UnblendedCost", unit="USD", end: date | None = None,
              estimated: bool = False) -> dict:
    return {"TimePeriod": {"Start": day.isoformat(), "End": (end or day + timedelta(days=1)).isoformat()}, "Estimated": estimated,
            "Total": {}, "Groups": [{"Keys": keys, "Metrics": {metric: {"Amount": str(a), "Unit": unit}}} for keys, a in groups]}


def ce_response(buckets: list[dict], next_token: str | None = None) -> dict:
    out: dict[str, Any] = {"ResultsByTime": buckets, "DimensionValueAttributes": []}
    if next_token:
        out["NextPageToken"] = next_token
    return out
