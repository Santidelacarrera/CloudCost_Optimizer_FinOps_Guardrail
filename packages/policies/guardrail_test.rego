package cloudcost.guardrail_test

import data.cloudcost.guardrail
import rego.v1

delete_change(address, typ, tags) := {"address": address, "type": typ, "change": {"actions": ["delete"], "before": {"tags": tags}, "after": null}}

resize_change(tags_before, tags_after) := {"address": "aws_instance.w", "type": "aws_instance", "change": {
	"actions": ["update"],
	"before": {"instance_type": "m5.2xlarge", "tags": tags_before},
	"after": {"instance_type": "m5.xlarge", "tags": tags_after},
}}

# ---------------------------------------------------------------- comportamiento original (CI del cliente)
test_delete_prod_denied if {
	count(guardrail.deny) == 1 with input as {"resource_changes": [delete_change("aws_instance.x", "aws_instance", {"Environment": "production"})]}
}

test_delete_prod_with_tag_allowed if {
	r := delete_change("aws_instance.x", "aws_instance", {"Environment": "production", "finops:reinforced-approval": "CHG-1"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

test_unknown_env_treated_as_prod if {
	count(guardrail.deny) == 1 with input as {"resource_changes": [delete_change("aws_ebs_volume.v", "aws_ebs_volume", {})]}
}

test_dev_delete_allowed if {
	count(guardrail.deny) == 0 with input as {"resource_changes": [delete_change("aws_ebs_volume.v", "aws_ebs_volume", {"Environment": "dev"})]}
}

test_prod_resize_needs_ticket if {
	r := resize_change({"Environment": "production"}, {"Environment": "production"})
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}

test_prod_resize_with_ticket_tag_allowed if {
	r := resize_change({"Environment": "production"}, {"Environment": "production", "finops:change-ticket": "CHG-9"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

# ---------------------------------------------------------------- endurecimiento: alias y valores raros
test_prod_aliases_are_production if {
	every alias in ["prod", "Prod", "PRD", "live", "Production"] {
		count(guardrail.deny) == 1 with input as {"resource_changes": [delete_change("aws_instance.x", "aws_instance", {"Environment": alias})]}
	}
}

test_unrecognized_environment_value_is_treated_as_prod if {
	count(guardrail.deny) == 1 with input as {"resource_changes": [delete_change("aws_instance.x", "aws_instance", {"Environment": "Prod-EU"})]}
}

test_nonprod_aliases_allowed if {
	every alias in ["dev", "staging", "STG", "qa", "sandbox"] {
		count(guardrail.deny) == 0 with input as {"resource_changes": [delete_change("aws_instance.x", "aws_instance", {"Environment": alias})]}
	}
}

test_other_resource_types_ignored if {
	count(guardrail.deny) == 0 with input as {"resource_changes": [delete_change("aws_s3_bucket.b", "aws_s3_bucket", {})]}
}

# ---------------------------------------------------------------- contexto que añade la API (input.cloudcost)
test_context_environment_overrides_tags if {
	r := delete_change("aws_instance.x", "aws_instance", {"env": "dev"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r], "cloudcost": {"environments": {"aws_instance.x": "development"}}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r], "cloudcost": {"environments": {"aws_instance.x": "production"}}}
}

test_context_unknown_environment_is_prod if {
	r := delete_change("aws_instance.x", "aws_instance", {})
	count(guardrail.deny) == 1 with input as {"resource_changes": [r], "cloudcost": {"environments": {"aws_instance.x": "unknown"}}}
}

test_reinforced_approval_from_platform_allows_prod_delete if {
	r := delete_change("aws_instance.x", "aws_instance", {"Environment": "production"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r], "cloudcost": {"reinforced_approval": true}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r], "cloudcost": {"reinforced_approval": false}}
}

test_change_ticket_from_approval_allows_prod_resize if {
	r := resize_change({"Environment": "production"}, {"Environment": "production"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r], "cloudcost": {"change_ticket": "CHG-1234"}}
}

test_staging_resize_needs_no_ticket if {
	r := resize_change({"Environment": "staging"}, {"Environment": "staging"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

test_resize_with_same_type_is_not_a_resize if {
	r := {"address": "aws_instance.w", "type": "aws_instance", "change": {
		"actions": ["update"],
		"before": {"instance_type": "m5.large", "tags": {"Environment": "production"}},
		"after": {"instance_type": "m5.large", "tags": {"Environment": "production", "x": "y"}},
	}}
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

test_no_changes_no_denies if {
	count(guardrail.deny) == 0 with input as {"resource_changes": []}
}

# ---------------------------------------------------------------- null y vacío no autorizan nada
test_null_or_empty_ticket_does_not_authorize_resize if {
	r := resize_change({"Environment": "production"}, {"Environment": "production"})
	count(guardrail.deny) == 1 with input as {"resource_changes": [r], "cloudcost": {"change_ticket": null}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r], "cloudcost": {"change_ticket": ""}}
}

test_empty_tags_do_not_authorize if {
	d := delete_change("aws_instance.x", "aws_instance", {"Environment": "production", "finops:reinforced-approval": ""})
	count(guardrail.deny) == 1 with input as {"resource_changes": [d]}
	r := resize_change({"Environment": "production"}, {"Environment": "production", "finops:change-ticket": ""})
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}

test_reinforced_approval_must_be_exactly_true if {
	d := delete_change("aws_instance.x", "aws_instance", {"Environment": "production"})
	count(guardrail.deny) == 1 with input as {"resource_changes": [d], "cloudcost": {"reinforced_approval": "yes"}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [d], "cloudcost": {"reinforced_approval": null}}
}

# ---------------------------------------------------------------- todas las nubes y las bases de datos (antes quedaban SIN proteger)
test_delete_prod_database_denied if {
	r := delete_change("aws_db_instance.orders", "aws_db_instance", {"Environment": "production"})
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}

test_delete_dev_database_allowed if {
	r := delete_change("aws_db_instance.orders", "aws_db_instance", {"Environment": "dev"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

test_every_destructive_type_is_protected_in_production if {
	every typ in guardrail.destructive_types {
		count(guardrail.deny) == 1 with input as {"resource_changes": [delete_change(concat(".", [typ, "x"]), typ, {"Environment": "production"})]}
	}
}

test_every_destructive_type_may_be_deleted_in_development if {
	every typ in guardrail.destructive_types {
		count(guardrail.deny) == 0 with input as {"resource_changes": [delete_change(concat(".", [typ, "x"]), typ, {"Environment": "dev"})]}
	}
}

test_azure_and_gcp_resize_in_prod_needs_ticket if {
	az := {"address": "azurerm_linux_virtual_machine.a", "type": "azurerm_linux_virtual_machine", "change": {
		"actions": ["update"],
		"before": {"size": "Standard_D8s_v5", "tags": {"Environment": "production"}},
		"after": {"size": "Standard_D4s_v5", "tags": {"Environment": "production"}},
	}}
	gcp := {"address": "google_compute_instance.g", "type": "google_compute_instance", "change": {
		"actions": ["update"],
		"before": {"machine_type": "n2-standard-8", "labels": {"environment": "production"}},
		"after": {"machine_type": "n2-standard-4", "labels": {"environment": "production"}},
	}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [az]}
	count(guardrail.deny) == 1 with input as {"resource_changes": [gcp]}
	count(guardrail.deny) == 0 with input as {"resource_changes": [az, gcp], "cloudcost": {"change_ticket": "CHG-5"}}
}

test_gcp_labels_define_the_environment if {
	prod := {"address": "google_compute_disk.d", "type": "google_compute_disk", "change": {"actions": ["delete"], "before": {"labels": {"environment": "production"}}, "after": null}}
	dev := {"address": "google_compute_disk.d", "type": "google_compute_disk", "change": {"actions": ["delete"], "before": {"labels": {"environment": "dev"}}, "after": null}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [prod]}
	count(guardrail.deny) == 0 with input as {"resource_changes": [dev]}
}

test_lowercase_environment_tag_is_recognised if {
	r := delete_change("azurerm_managed_disk.m", "azurerm_managed_disk", {"environment": "dev"})
	count(guardrail.deny) == 0 with input as {"resource_changes": [r]}
}

# ---------------------------------------------------------------- varios cambios en un mismo plan
test_each_violating_change_is_reported_separately if {
	a := delete_change("aws_instance.a", "aws_instance", {"Environment": "production"})
	b := delete_change("aws_db_instance.b", "aws_db_instance", {"Environment": "production"})
	c := delete_change("aws_instance.c", "aws_instance", {"Environment": "dev"})
	count(guardrail.deny) == 2 with input as {"resource_changes": [a, b, c]}
}

test_replace_counts_as_delete if {
	r := {"address": "aws_instance.x", "type": "aws_instance", "change": {"actions": ["delete", "create"], "before": {"tags": {"Environment": "production"}}, "after": {}}}
	count(guardrail.deny) == 1 with input as {"resource_changes": [r]}
}
