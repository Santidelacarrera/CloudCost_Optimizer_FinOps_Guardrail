# Cuenta de servicio de SOLO LECTURA para CloudCost Optimizer: «Compute Viewer» y «Monitoring Viewer» sobre UN proyecto.
# No pueden crear, modificar ni borrar recursos. La clave JSON NO la crea Terraform (acabaría en el estado): ver docs/onboarding-azure-gcp.md.
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.0" }
  }
}

variable "project_id" {
  type        = string
  description = "Proyecto que CloudCost podrá leer."
}
variable "account_id" {
  type    = string
  default = "cloudcost-readonly"
}

provider "google" {
  project = var.project_id
}

resource "google_service_account" "cloudcost" {
  account_id   = var.account_id
  display_name = "CloudCost Optimizer (solo lectura)"
}

resource "google_project_iam_member" "compute_viewer" {
  project = var.project_id
  role    = "roles/compute.viewer"
  member  = "serviceAccount:${google_service_account.cloudcost.email}"
}

resource "google_project_iam_member" "monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.cloudcost.email}"
}

output "service_account_email" { value = google_service_account.cloudcost.email }
output "project_id" { value = var.project_id }
