# Guardrail sobre un plan de Terraform (`terraform show -json plan.tfplan`): bloquea los cambios de CloudCost que
# destruyan o redimensionen recursos de producción sin la autorización correspondiente.
#
# Se evalúa en DOS sitios con exactamente estas mismas reglas:
#   1. Dentro de la API, de forma síncrona, ANTES de crear el Pull Request (sin depender del CI del cliente). La API
#      construye un plan sintético a partir del parche IaC y añade el contexto `input.cloudcost` (ver más abajo).
#   2. En el CI del cliente, sobre el plan real (`opa eval`/`opa test`), donde `input.cloudcost` no existe.
#
# Contexto opcional que añade la API (`input.cloudcost`):
#   environments:        { "<dirección Terraform>": "production|staging|development|test|unknown" }  (entorno normalizado)
#   reinforced_approval: true si la aprobación reforzada (mínimo 2 aprobadores, uno ADMIN o SRE) ya se completó
#   change_ticket:       ticket citado en el motivo de la aprobación (p. ej. "CHG-1234"), si lo hubo
package cloudcost.guardrail

import rego.v1

destructive_types := {"aws_instance", "aws_ebs_volume", "aws_ebs_snapshot"}

prod_values := {"production", "prod", "prd", "live"}

nonprod_values := {"dev", "development", "sandbox", "test", "testing", "qa", "stg", "stage", "staging", "preprod", "uat"}

changes contains rc if {
	some rc in input.resource_changes
	rc.type in destructive_types
}

# Entorno del recurso: lo que normaliza la API; si no hay contexto, se deduce de la etiqueta Environment.
# Un valor desconocido o ausente se trata como producción (la opción prudente).
environment(rc) := env if {
	env := input.cloudcost.environments[rc.address]
} else := "production" if {
	lower(rc.change.before.tags.Environment) in prod_values
} else := "non-production" if {
	lower(rc.change.before.tags.Environment) in nonprod_values
} else := "unknown"

is_prod(rc) if environment(rc) in {"production", "unknown"}

# Una etiqueta o un ticket solo cuentan si son texto NO vacío (en Rego `null` y "" también son valores "definidos").
non_empty(v) if {
	is_string(v)
	v != ""
}

has_reinforced_approval(rc) if non_empty(rc.change.before.tags["finops:reinforced-approval"])

has_reinforced_approval(_) if input.cloudcost.reinforced_approval == true

has_change_ticket(rc) if non_empty(rc.change.after.tags["finops:change-ticket"])

has_change_ticket(_) if non_empty(input.cloudcost.change_ticket)

deny contains msg if {
	some rc in changes
	"delete" in rc.change.actions
	is_prod(rc)
	not has_reinforced_approval(rc)
	msg := sprintf("%s: eliminar un recurso de producción requiere la etiqueta finops:reinforced-approval o la aprobación reforzada completada", [rc.address])
}

deny contains msg if {
	some rc in changes
	rc.change.actions == ["update"]
	rc.type == "aws_instance"
	is_prod(rc)
	rc.change.before.instance_type != rc.change.after.instance_type
	not has_change_ticket(rc)
	msg := sprintf("%s: el cambio de tamaño en producción requiere finops:change-ticket o citar el ticket (p. ej. CHG-1234) en el motivo de la aprobación", [rc.address])
}
