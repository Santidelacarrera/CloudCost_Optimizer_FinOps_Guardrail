from cloudcost.collectors.file_import import TEMPLATE_CSV, parse_csv, rows_to_resources
from cloudcost.domain.rules import RuleConfig, evaluate_all


def test_template_parses_and_yields_findings():
    res = parse_csv(TEMPLATE_CSV)
    assert res.errors == [] and len(res.rows) == 3
    resources = rows_to_resources(res.rows)
    findings = evaluate_all(resources, RuleConfig())
    actions = {f.action for f in findings}
    assert "RESIZE_INSTANCE" in actions and "DELETE_VOLUME" in actions and "DELETE_SNAPSHOT" in actions


def test_semicolon_and_spanish_headers():
    csv_text = "id;servicio;tipo_instancia;cpu_promedio;memoria_promedio;dias_observacion\ni-1;ec2;m5.2xlarge;10;20;30\n"
    res = parse_csv(csv_text)
    assert res.errors == [] and res.rows[0]["instance_type"] == "m5.2xlarge" and res.rows[0]["cpu_avg"] == 10.0


def test_validation_errors_are_reported_not_stored():
    bad = "resource_id,service,cpu_avg\n../x,ec2,150\ni-2,rds,5\ni-3,ec2,5\ni-3,ec2,5\n"
    res = parse_csv(bad)
    assert res.rows == [] and len(res.errors) >= 3


def test_missing_required_columns_and_empty():
    assert parse_csv("a,b\n1,2\n").errors
    assert parse_csv("").errors


def test_limits():
    big = "resource_id,service,size_gb\n" + "".join(f"vol-{i},ebs,10\n" for i in range(5001))
    res = parse_csv(big)
    assert any("máximo" in e for e in res.errors)
