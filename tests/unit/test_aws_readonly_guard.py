"""La plataforma nunca escribe en la nube: segundo cerrojo (en código) además del rol IAM de solo lectura del cliente."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from aws_stub import ACCOUNT, TODAY, StubbedSession
from cloudcost.collectors import aws_guard
from cloudcost.collectors.aws import AwsCollector
from cloudcost.collectors.aws_guard import ALLOWED_OPERATIONS, ReadOnlyViolation

COLLECTORS = Path(__file__).resolve().parents[2] / "apps" / "api" / "cloudcost" / "collectors"


class _Secrets:
    def resolve(self, ref, org_id=None):
        return None


def _collector():
    session = StubbedSession()
    return AwsCollector({"regions": ["us-east-1"], "role_arn": None, "account_ref": ACCOUNT}, _Secrets(), use_cost_explorer=True,
                        session=session, today=lambda: TODAY, sleep=lambda s: None), session


WRITES = [
    ("ec2", "terminate_instances", {"InstanceIds": ["i-1"]}), ("ec2", "stop_instances", {"InstanceIds": ["i-1"]}),
    ("ec2", "run_instances", {"MaxCount": 1, "MinCount": 1}), ("ec2", "modify_instance_attribute", {"InstanceId": "i-1"}),
    ("ec2", "delete_volume", {"VolumeId": "vol-1"}), ("ec2", "delete_snapshot", {"SnapshotId": "snap-1"}),
    ("ec2", "create_snapshot", {"VolumeId": "vol-1"}), ("ec2", "create_tags", {"Resources": ["i-1"], "Tags": []}),
    ("ec2", "attach_volume", {"Device": "/dev/sdf", "InstanceId": "i-1", "VolumeId": "vol-1"}),
    ("cloudwatch", "put_metric_data", {"Namespace": "x", "MetricData": []}), ("cloudwatch", "delete_alarms", {"AlarmNames": ["a"]}),
    ("cloudtrail", "stop_logging", {"Name": "t"}), ("cloudtrail", "delete_trail", {"Name": "t"}),
    ("ce", "create_anomaly_monitor", {"AnomalyMonitor": {"MonitorName": "m", "MonitorType": "DIMENSIONAL"}}),
    ("ce", "delete_anomaly_monitor", {"MonitorArn": "arn:aws:ce::1:anomalymonitor/x"}),
    ("sts", "assume_role", {"RoleArn": "arn:aws:iam::1:role/x", "RoleSessionName": "x"}),
]

UNLISTED_READS = [("ec2", "describe_security_groups", {}), ("ec2", "describe_instance_types", {}), ("cloudwatch", "describe_alarms", {}),
                  ("ce", "get_dimension_values", {"TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"}, "Dimension": "SERVICE"}),
                  ("ce", "get_anomalies", {"DateInterval": {"StartDate": "2026-01-01"}})]


@pytest.mark.parametrize("service,method,kwargs", WRITES + UNLISTED_READS)
def test_operations_outside_the_closed_list_are_rejected_before_any_request_is_built(service, method, kwargs):
    c, session = _collector()
    client = c._client(service, "us-east-1")
    with pytest.raises(ReadOnlyViolation, match="no permitida"):
        getattr(client, method)(**kwargs)
    assert session.calls == []                                   # nada llegó siquiera a la capa de transporte


def test_the_guard_is_idempotent_and_every_allowed_operation_passes_it():
    c, _ = _collector()
    c._boto_session()
    c._boto_session()                                            # registrar dos veces no duplica el manejador
    for service, operation in ALLOWED_OPERATIONS:
        aws_guard.check_operation(service, operation)


def test_allowed_list_contains_only_read_style_operations():
    for service, operation in ALLOWED_OPERATIONS:
        assert re.match(r"^(Describe|Get|List|Lookup)", operation), f"{service}:{operation}"
    assert all(a == "" or re.match(r"^[a-z0-9-]+:(Describe|Get|List|Lookup)", a) for a in ALLOWED_OPERATIONS.values())


def test_collector_sources_contain_no_aws_write_calls():
    """Red de seguridad estática: ningún verbo de escritura de boto3 aparece en el código de los colectores de AWS."""
    verbs = re.compile(r"\.(create|delete|modify|put|terminate|run|start|stop|attach|detach|update|tag|untag|reboot|assume|invoke|"
                       r"authorize|revoke|associate|disassociate|register|deregister|enable|disable|import|copy|restore|reset)_[a-z_]+\(")
    offenders = {}
    for name in ("aws.py", "aws_costs.py", "aws_errors.py"):
        text = (COLLECTORS / name).read_text(encoding="utf-8")
        hits = [m.group(0) for m in verbs.finditer(text)]
        if hits:
            offenders[name] = hits
    # assume_role: único permitido y fuera de la sesión guardada (se usa para OBTENER la sesión de solo lectura)
    assert offenders == {"aws.py": [".assume_role("]}, offenders


def test_a_collection_run_installs_the_guard_before_creating_the_first_client():
    c, session = _collector()
    assert not session.clients
    c._client("ec2", "us-east-1")
    with pytest.raises(ReadOnlyViolation):
        session.clients["ec2"].delete_volume(VolumeId="vol-1")
