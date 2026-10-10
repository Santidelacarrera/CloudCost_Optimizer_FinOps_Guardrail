"""Importación de inventario por archivo: RDS, Azure (vm/disk/disk_snapshot), GCP (gce/pd/pd_snapshot) y cargas de Kubernetes."""
from __future__ import annotations

import pytest
from cloudcost.collectors.file_import import SERVICES, TEMPLATE_FULL_CSV, parse_csv, rows_to_resources
from cloudcost.domain.rules import RuleConfig, evaluate_all

HEADER = "resource_id,service,instance_type,volume_type,size_gb,monthly_cost,cpu_avg,observation_days,attached,unattached_days,namespace,workload,cpu_request,mem_request,connections_avg,cpu_p95_cores,cpu_max_cores,mem_max"


def test_full_template_parses_and_every_family_produces_the_expected_findings():
    parsed = parse_csv(TEMPLATE_FULL_CSV)
    assert parsed.errors == [] and {r["service"] for r in parsed.rows} == {"ec2", "rds", "vm", "disk", "pd", "k8s_workload"}
    resources = rows_to_resources(parsed.rows)
    assert {(r.provider, r.service) for r in resources} == {("aws", "ec2"), ("aws", "rds"), ("azure", "vm"), ("azure", "disk"), ("gcp", "pd"),
                                                           ("kubernetes", "k8s_workload")}
    findings = {(f.rule_id, f.resource.service) for f in evaluate_all(resources, RuleConfig())}
    assert findings == {("ec2_downsize", "ec2"), ("rds_idle", "rds"), ("ebs_orphan", "disk"), ("ebs_orphan", "pd"), ("k8s_overprovisioned", "k8s_workload")}
    rds = next(f for f in evaluate_all(resources, RuleConfig()) if f.rule_id == "rds_idle")
    assert rds.destructive is True                                         # eliminar una base de datos sigue siendo destructivo


def test_kubernetes_row_composes_its_id_and_accepts_units():
    text = HEADER + "\n,k8s_workload,,,,,,14,,,shop,api,500m,512Mi,,0.03,0.1,60Mi\n"
    parsed = parse_csv(text)
    assert parsed.errors == []
    (res,) = rows_to_resources(parsed.rows)
    assert res.resource_id == "import/shop/Deployment/api/app" and res.attributes["cpu_request"] == 0.5 and res.attributes["mem_request"] == 512 * 2**20
    assert res.attributes["mem_max_bytes"] == 60 * 2**20 and res.monthly_cost > 0


@pytest.mark.parametrize("row,fragment", [
    (",k8s_workload,,,,,,14,,,shop,api,,512Mi,,,,", "requiere cpu_request"),
    (",k8s_workload,,,,,,14,,,shop,api,500x,512Mi,,,,", "no es una cantidad válida"),
    ("db1,rds,,,100,,,30,,,,,,,0,,,", "rds requiere instance_type"),
    ("vm1,vm,Standard_D2s_v3,,,,5,30,,,,,,,,,,", "vm requiere monthly_cost"),
    ("d1,disk,,premium_lrs,,,,30,false,40,,,,,,,,", "disk requiere size_gb"),
    ("x1,s3,,,,,,,,,,,,,,,,", "service debe ser uno de"),
])
def test_invalid_rows_are_rejected_with_a_clear_message(row, fragment):
    parsed = parse_csv(HEADER + "\n" + row + "\n")
    assert parsed.rows == [] and any(fragment in e for e in parsed.errors), parsed.errors


def test_every_imported_service_has_a_rule_that_can_use_it():
    """Solo se admiten servicios para los que existe una regla: un servicio sin regla no produciría nunca una recomendación."""
    from cloudcost.domain.models import COMPUTE_SERVICES, SNAPSHOT_SERVICES, VOLUME_SERVICES

    covered = COMPUTE_SERVICES | VOLUME_SERVICES | SNAPSHOT_SERVICES | {"rds", "k8s_workload"}
    assert SERVICES <= covered
