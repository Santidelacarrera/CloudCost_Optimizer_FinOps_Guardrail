# Service principal de SOLO LECTURA para CloudCost Optimizer: roles integrados «Reader» y «Monitoring Reader» sobre UNA suscripción.
# Ninguno de los dos permite crear, modificar ni borrar recursos, ni leer secretos (Key Vault, claves de almacenamiento).
terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = ">= 3.100" }
    azuread = { source = "hashicorp/azuread", version = ">= 3.0" }
  }
}

variable "subscription_id" {
  type        = string
  description = "Suscripción que CloudCost podrá leer (un service principal por suscripción)."
}
variable "display_name" {
  type    = string
  default = "cloudcost-optimizer-readonly"
}
variable "secret_lifetime_hours" {
  type        = number
  default     = 4380 # 6 meses: rota el secreto antes de que venza
  description = "Vigencia del secreto del service principal."
}

provider "azurerm" {
  subscription_id = var.subscription_id
  features {}
}

resource "azuread_application" "cloudcost" {
  display_name = var.display_name
}

resource "azuread_service_principal" "cloudcost" {
  client_id = azuread_application.cloudcost.client_id
}

resource "azuread_application_password" "cloudcost" {
  application_id = azuread_application.cloudcost.id
  display_name   = "cloudcost"
  end_date       = timeadd(timestamp(), "${var.secret_lifetime_hours}h")
  lifecycle { ignore_changes = [end_date] }
}

locals {
  scope = "/subscriptions/${var.subscription_id}"
}

resource "azurerm_role_assignment" "reader" {
  scope                = local.scope
  role_definition_name = "Reader"
  principal_id         = azuread_service_principal.cloudcost.object_id
}

resource "azurerm_role_assignment" "monitoring_reader" {
  scope                = local.scope
  role_definition_name = "Monitoring Reader"
  principal_id         = azuread_service_principal.cloudcost.object_id
}

data "azuread_client_config" "current" {}

output "tenant_id" { value = data.azuread_client_config.current.tenant_id }
output "client_id" { value = azuread_application.cloudcost.client_id }
output "subscription_id" { value = var.subscription_id }
# El secreto queda en el estado de Terraform: guarda el estado cifrado o crea el secreto con `az` (ver docs/onboarding-azure-gcp.md).
output "client_secret" {
  value     = azuread_application_password.cloudcost.value
  sensitive = true
}
