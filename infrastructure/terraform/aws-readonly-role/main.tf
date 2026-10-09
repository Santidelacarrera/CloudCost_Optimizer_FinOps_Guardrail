# Rol IAM de SOLO LECTURA que CloudCost Optimizer asume (sts:AssumeRole + ExternalId). No concede ninguna escritura.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.0" }
  }
}

variable "trusted_principal_arn" {
  type        = string
  description = "ARN del rol/usuario de CloudCost (cuenta de la plataforma) autorizado a asumir este rol"
}
variable "external_id" {
  type        = string
  sensitive   = true
  description = "ExternalId (anti confused-deputy); guárdalo en tu gestor de secretos y referénciarlo como aws-sm:cloudcost/<org_id>/..."
}
variable "enable_cost_explorer" {
  type        = bool
  default     = true
  description = "Permite leer Cost Explorer (ce:GetCostAndUsage[WithResources]) para usar el costo REAL de cada recurso en lugar de la tabla de precios. Cada llamada de Cost Explorer cuesta USD 0.01 en la cuenta de pagos."
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [var.trusted_principal_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.external_id]
    }
  }
}

resource "aws_iam_role" "readonly" {
  name                 = "CloudCostOptimizerReadOnly"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "read" {
  statement {
    sid = "ReadInventoryAndMetrics"
    actions = [
      "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:DescribeImages",
      "cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudtrail:LookupEvents",
    ]
    resources = ["*"]
  }
  dynamic "statement" {
    for_each = var.enable_cost_explorer ? [1] : []
    content {
      sid       = "ReadCosts"
      actions   = ["ce:GetCostAndUsage", "ce:GetCostAndUsageWithResources"]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "read" {
  name   = "readonly"
  role   = aws_iam_role.readonly.id
  policy = data.aws_iam_policy_document.read.json
}

output "role_arn" {
  value = aws_iam_role.readonly.arn
}
