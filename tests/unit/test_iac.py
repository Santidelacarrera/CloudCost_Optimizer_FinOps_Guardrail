from pathlib import Path

from cloudcost.domain.models import NormalizedResource
from cloudcost.domain.rules import ACTION_DELETE_VOLUME, ACTION_REMOVE, ACTION_RESIZE
from cloudcost.iac.patcher import PatchError, build_patch
from cloudcost.iac.terraform import IacIndex, parse_resources, state_ids_from_tfstate

EXAMPLE = Path(__file__).resolve().parents[2] / "infrastructure/terraform/example-iac/main.tf"


def _index() -> IacIndex:
    return IacIndex.build({"main.tf": EXAMPLE.read_text()})


def test_parses_all_example_resources():
    idx = _index()
    addresses = {b.address for b in idx.blocks}
    assert {"aws_instance.web", "aws_instance.api", "aws_instance.batch", "aws_instance.reports",
            "aws_ebs_volume.api_data", "aws_ebs_volume.legacy_data", "aws_ebs_volume.scratch",
            "aws_ebs_snapshot.old_backup", "aws_volume_attachment.api_data"} <= addresses
    assert not idx.parse_errors


def test_attr_literal_and_name_tag():
    web = _index().block_by_address("aws_instance.web")
    assert web.attr_literal("instance_type")[0] == "m5.2xlarge"
    assert web.name_tag == "web-prod-1"
    api = _index().block_by_address("aws_instance.api")
    assert api.attr_literal("instance_type") is None          # var.api_instance_type no es literal


def test_nested_values_are_not_top_level():
    text = 'resource "aws_instance" "x" {\n  root_block_device {\n    instance_type = "bogus"\n  }\n  instance_type = "t3.small"\n}\n'
    (block,) = parse_resources(text)
    assert block.attr_literal("instance_type")[0] == "t3.small"


def test_braces_in_strings_comments_and_heredocs():
    text = (
        'resource "aws_instance" "a" {\n'
        '  user_data = <<-EOT\n    echo "}" }\n  EOT\n'
        '  # comentario con } llave\n'
        '  tags = { Name = "x-${lookup(var.m, "k")}" }\n'
        '  instance_type = "t3.micro"\n}\n'
        'resource "aws_instance" "b" {\n  instance_type = "t3.small"\n}\n'
    )
    blocks = parse_resources(text)
    assert [b.name for b in blocks] == ["a", "b"]
    assert blocks[0].attr_literal("instance_type")[0] == "t3.micro"
    assert blocks[1].attr_literal("instance_type")[0] == "t3.small"


def test_match_by_name_tag_and_ambiguity():
    idx = _index()
    res = NormalizedResource("aws", "compute", "ec2", "i-1", "us-east-1", tags={"Name": "web-prod-1"})
    assert idx.match(res).address == "aws_instance.web"
    unknown = NormalizedResource("aws", "compute", "ec2", "i-2", "us-east-1", tags={"Name": "nope"})
    assert idx.match(unknown) is None
    dup = IacIndex.build({"a.tf": 'resource "aws_instance" "a" {\n tags = { Name = "same" }\n}\n'
                                  'resource "aws_instance" "b" {\n tags = { Name = "same" }\n}\n'})
    assert dup.match(NormalizedResource("aws", "compute", "ec2", "i-3", "r", tags={"Name": "same"})) is None


def test_state_mapping_has_priority():
    idx = IacIndex.build({"main.tf": EXAMPLE.read_text()}, state_ids={"i-999": "aws_instance.batch"})
    res = NormalizedResource("aws", "compute", "ec2", "i-999", "us-east-1", tags={"Name": "otra-cosa"})
    assert idx.match(res).address == "aws_instance.batch"
    state = {"resources": [{"mode": "managed", "type": "aws_instance", "name": "web", "instances": [{"attributes": {"id": "i-1"}}]},
                           {"mode": "managed", "type": "aws_instance", "name": "many",
                            "instances": [{"index_key": 0, "attributes": {"id": "i-2"}}, {"index_key": 1, "attributes": {"id": "i-3"}}]}]}
    assert state_ids_from_tfstate(state) == {"i-1": "aws_instance.web"}


def test_resize_patch_changes_only_the_attribute():
    idx = _index()
    block = idx.block_by_address("aws_instance.web")
    result = build_patch(action=ACTION_RESIZE, block=block, index=idx,
                         params={"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"})
    changed = [l for l in result.diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    assert changed == ['-  instance_type = "m5.2xlarge"', '+  instance_type = "m5.xlarge"']
    assert result.validations[0]["passed"]
    assert "m5.xlarge" in result.new_text and result.new_text.count("m5.2xlarge") == 0


def test_resize_refuses_drift_and_non_literal():
    idx = _index()
    web = idx.block_by_address("aws_instance.web")
    try:
        build_patch(action=ACTION_RESIZE, block=web, index=idx,
                    params={"current_instance_type": "m5.4xlarge", "target_instance_type": "m5.xlarge"})
        raise AssertionError("debió fallar por deriva")
    except PatchError as e:
        assert e.code == "drift"
    api = idx.block_by_address("aws_instance.api")
    try:
        build_patch(action=ACTION_RESIZE, block=api, index=idx,
                    params={"current_instance_type": "t3.large", "target_instance_type": "t3.medium"})
        raise AssertionError("debió fallar por no literal")
    except PatchError as e:
        assert e.code == "not_literal"


def test_resize_refuses_malicious_target():
    idx = _index()
    web = idx.block_by_address("aws_instance.web")
    try:
        build_patch(action=ACTION_RESIZE, block=web, index=idx,
                    params={"current_instance_type": "m5.2xlarge", "target_instance_type": 'x" \n provisioner "local-exec" {'})
        raise AssertionError("debió rechazar")
    except PatchError as e:
        assert e.code == "invalid_params"


def test_remove_block_and_dependency_guard():
    idx = _index()
    scratch = idx.block_by_address("aws_ebs_volume.scratch")
    result = build_patch(action=ACTION_DELETE_VOLUME, block=scratch, index=idx, params={})
    assert "scratch-dev" not in result.new_text
    assert "legacy-data-prod" in result.new_text
    parse_resources(result.new_text)                      # sigue siendo válido
    api_data = idx.block_by_address("aws_ebs_volume.api_data")      # referenciado por attachment y snapshots
    try:
        build_patch(action=ACTION_DELETE_VOLUME, block=api_data, index=idx, params={})
        raise AssertionError("debió fallar por referencias")
    except PatchError as e:
        assert e.code == "referenced"


def test_multi_instance_is_refused():
    text = 'resource "aws_instance" "w" {\n  count = 3\n  instance_type = "m5.large"\n}\n'
    idx = IacIndex.build({"m.tf": text})
    block = idx.block_by_address("aws_instance.w")
    assert block.multi_instance
    try:
        build_patch(action=ACTION_REMOVE, block=block, index=idx, params={})
        raise AssertionError
    except PatchError as e:
        assert e.code == "multi_instance"
