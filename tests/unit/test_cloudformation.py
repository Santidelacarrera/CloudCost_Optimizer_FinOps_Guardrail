"""La plantilla de CloudFormation no puede divergir del Terraform ni conceder nada más que lectura."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from cloudcost import onboarding

ROOT = Path(__file__).resolve().parents[2]
CFN_PATH = ROOT / "infrastructure" / "cloudformation" / "readonly-role.yaml"
TF = (ROOT / "infrastructure" / "terraform" / "aws-readonly-role" / "main.tf").read_text(encoding="utf-8")
CFN = yaml.safe_load(CFN_PATH.read_text(encoding="utf-8"))
ROLE = CFN["Resources"]["ReadOnlyRole"]["Properties"]
POLICY = ROLE["Policies"][0]["PolicyDocument"]["Statement"]
ACTION = re.compile(r'"([a-z0-9-]+:[A-Za-z*]+)"')
PRINCIPAL = "arn:aws:iam::123456789012:role/CloudCostPlatform"
TEMPLATE_URL = "https://cloudcost-templates.s3.amazonaws.com/readonly-role.yaml"


def _cfn_statements() -> list[dict]:
    """Aplana el Fn::If de Cost Explorer: devuelve las sentencias con su condición (None = siempre)."""
    out = []
    for st in POLICY:
        if "Fn::If" in st:
            cond, then, otherwise = st["Fn::If"]
            assert otherwise == {"Ref": "AWS::NoValue"}, "sin Cost Explorer no debe añadirse ninguna sentencia"
            out.append((cond, then))
        else:
            out.append((None, st))
    return out


def _tf_block(start: str, end: str) -> set[str]:
    return set(ACTION.findall(TF[TF.index(start):TF.index(end)]))


def test_las_acciones_de_inventario_son_las_mismas_que_en_terraform():
    inventory = next(st for cond, st in _cfn_statements() if cond is None)
    tf = _tf_block('sid = "ReadInventoryAndMetrics"', 'dynamic "statement"')
    assert set(inventory["Action"]) == tf and len(tf) >= 10


def test_las_acciones_de_cost_explorer_son_las_mismas_que_en_terraform_y_son_condicionales():
    (cond, costs), = [(c, s) for c, s in _cfn_statements() if c]
    assert cond == "CostExplorerEnabled"
    assert set(costs["Action"]) == _tf_block('sid       = "ReadCosts"', "resource \"aws_iam_role_policy\"")
    assert CFN["Conditions"]["CostExplorerEnabled"] == {"Fn::Equals": [{"Ref": "EnableCostExplorer"}, "true"]}


def test_solo_lectura_ninguna_accion_de_escritura_ni_comodines():
    actions = [a for _, st in _cfn_statements() for a in st["Action"]]
    assert actions
    for action in actions:
        service, verb = action.split(":")
        assert "*" not in action, action
        assert re.match(r"^(Describe|Get|List|Lookup)", verb), f"{action} no es una acción de lectura"
        assert service in {"ec2", "cloudwatch", "cloudtrail", "backup", "dlm", "ce"}
    assert all(st["Effect"] == "Allow" for _, st in _cfn_statements())


def test_la_confianza_exige_externalid_y_un_principal_concreto():
    trust = ROLE["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    st = trust[0]
    assert st["Action"] == "sts:AssumeRole" and st["Effect"] == "Allow"
    assert st["Principal"] == {"AWS": {"Ref": "TrustedPrincipalArn"}}
    assert st["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}
    assert ROLE["MaxSessionDuration"] == 3600


def test_parametros_seguros_y_salida():
    params = CFN["Parameters"]
    assert params["ExternalId"]["NoEcho"] is True and params["ExternalId"]["MinLength"] >= 16
    assert params["EnableCostExplorer"]["AllowedValues"] == ["true", "false"]
    assert params["RoleName"]["Default"] == "CloudCostOptimizerReadOnly" and 'name                 = "CloudCostOptimizerReadOnly"' in TF
    pattern = re.compile(params["TrustedPrincipalArn"]["AllowedPattern"])
    assert pattern.match(PRINCIPAL) and pattern.match("arn:aws:iam::123456789012:root")
    for bad in ("*", "arn:aws:iam::*:root", "123456789012", "arn:aws:iam::123456789012:role/"):
        assert not pattern.match(bad), bad
    assert "RoleArn" in CFN["Outputs"]
    assert "ExternalId" not in CFN["Outputs"], "el ExternalId no debe reaparecer en las salidas de la pila"


def test_el_patron_del_externalid_acepta_los_generados():
    pattern = re.compile(CFN["Parameters"]["ExternalId"]["AllowedPattern"])
    for _ in range(200):
        value = onboarding.new_external_id()
        assert pattern.match(value) and len(value) >= CFN["Parameters"]["ExternalId"]["MinLength"]
    assert not pattern.match("con espacios y *")


def test_externalid_distintos_cada_vez():
    assert len({onboarding.new_external_id() for _ in range(100)}) == 100


def test_quick_create_url_lleva_la_plantilla_y_los_parametros():
    url = onboarding.quick_create_url(template_url=TEMPLATE_URL, principal_arn=PRINCIPAL, external_id="cc-abc", region="eu-west-1",
                                      enable_cost_explorer=False)
    parsed = urlparse(url)
    assert parsed.netloc == "eu-west-1.console.aws.amazon.com" and parsed.path == "/cloudformation/home"
    params = parse_qs(parsed.fragment.split("?", 1)[1])
    assert params["templateURL"] == [TEMPLATE_URL]
    assert params["param_TrustedPrincipalArn"] == [PRINCIPAL] and params["param_ExternalId"] == ["cc-abc"]
    assert params["param_EnableCostExplorer"] == ["false"]
    declared = set(CFN["Parameters"])
    assert {k.removeprefix("param_") for k in params if k.startswith("param_")} <= declared


@pytest.mark.parametrize("template", [None, "", "http://x.s3.amazonaws.com/t.yaml", "https://evil.example.com/t.yaml", "ftp://x.s3.amazonaws.com/t"])
def test_plantilla_no_valida(template):
    with pytest.raises(onboarding.OnboardingError):
        onboarding.quick_create_url(template_url=template or "", principal_arn=PRINCIPAL, external_id="cc-abc")


@pytest.mark.parametrize("principal", ["*", "arn:aws:iam::*:root", "arn:aws:iam::123:role/x", ""])
def test_principal_no_valido(principal):
    with pytest.raises(onboarding.OnboardingError):
        onboarding.quick_create_url(template_url=TEMPLATE_URL, principal_arn=principal, external_id="cc-abc")


def test_region_no_valida():
    with pytest.raises(onboarding.OnboardingError):
        onboarding.quick_create_url(template_url=TEMPLATE_URL, principal_arn=PRINCIPAL, external_id="cc-abc", region="x.evil.com/")


def test_launch_info_completo_y_exige_configuracion():
    info = onboarding.launch_info(template_url=TEMPLATE_URL, principal_arn=PRINCIPAL, region="us-east-1")
    assert info["external_id"] in info["quick_create_url"] and len(info["next_steps"]) == 3
    with pytest.raises(onboarding.OnboardingError):
        onboarding.launch_info(template_url=TEMPLATE_URL, principal_arn=None, region="us-east-1")
    with pytest.raises(onboarding.OnboardingError):
        onboarding.launch_info(template_url=None, principal_arn=PRINCIPAL, region="us-east-1")
