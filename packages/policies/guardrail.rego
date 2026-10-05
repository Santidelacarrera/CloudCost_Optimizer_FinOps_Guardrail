# Guardrail sobre `terraform show -json plan.tfplan`: bloquea cambios de CloudCost que destruyan recursos
# de producción sin etiqueta de aprobación reforzada,.
package cloudcost.guardrail

import rego.v1

destructive_types := {"aws_instance", "aws_ebs_volume", "aws_ebs_snapshot"}

changes contains rc if {
	some rc in input.resource_changes
	rc.type in destructive_types
}

is_prod(rc) if rc.change.before.tags.Environment == "production"

is_prod(rc) if not rc.change.before.tags.Environment

deny contains msg if {
	some rc in changes
	"delete" in rc.change.actions
	is_prod(rc)
	not rc.change.before.tags["finops:reinforced-approval"]
	msg := sprintf("%s: eliminar un recurso de producción requiere la etiqueta finops:reinforced-approval", [rc.address])
}

deny contains msg if {
	some rc in changes
	rc.change.actions == ["update"]
	rc.type == "aws_instance"
	rc.change.after.tags.Environment == "production"
	rc.change.before.instance_type != rc.change.after.instance_type
	not rc.change.after.tags["finops:change-ticket"]
	msg := sprintf("%s: el cambio de tamaño en producción requiere finops:change-ticket", [rc.address])
}
