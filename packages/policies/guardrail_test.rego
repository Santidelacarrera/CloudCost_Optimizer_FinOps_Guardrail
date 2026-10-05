package cloudcost.guardrail_test

import data.cloudcost.guardrail
import rego.v1

test_delete_prod_denied if {
	r := {"address": "aws_instance.x", "type": "aws_instance", "change": {"actions": ["delete"], "before": {"tags": {"Environment": "production"}}, "after": null}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}

test_delete_prod_with_tag_allowed if {
	r := {"address": "aws_instance.x", "type": "aws_instance", "change": {"actions": ["delete"], "before": {"tags": {"Environment": "production", "finops:reinforced-approval": "CHG-1"}}, "after": null}}
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

test_unknown_env_treated_as_prod if {
	r := {"address": "aws_ebs_volume.v", "type": "aws_ebs_volume", "change": {"actions": ["delete"], "before": {"tags": {}}, "after": null}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}

test_dev_delete_allowed if {
	r := {"address": "aws_ebs_volume.v", "type": "aws_ebs_volume", "change": {"actions": ["delete"], "before": {"tags": {"Environment": "dev"}}, "after": null}}
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

test_prod_resize_needs_ticket if {
	r := {"address": "aws_instance.w", "type": "aws_instance", "change": {"actions": ["update"], "before": {"instance_type": "m5.2xlarge", "tags": {"Environment": "production"}}, "after": {"instance_type": "m5.xlarge", "tags": {"Environment": "production"}}}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}
