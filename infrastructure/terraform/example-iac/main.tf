# IaC de ejemplo: es el "repositorio del cliente" contra el que se prueba el flujo completo en modo demo.
# Los nombres (tag Name) coinciden con los recursos sintéticos de cloudcost/collectors/demo.py.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "ami_id" {
  type        = string
  description = "AMI base (Amazon Linux)"
  default     = "ami-0123456789abcdef0"
}

variable "subnet_id" {
  type    = string
  default = "subnet-0123456789abcdef0"
}

variable "api_instance_type" {
  type    = string
  default = "t3.large"
}

# --------------------------------------------------------------- Cómputo
resource "aws_instance" "web" {
  ami           = var.ami_id
  subnet_id     = var.subnet_id
  instance_type = "m5.2xlarge"

  tags = {
    Name        = "web-prod-1"
    Environment = "production"
  }
}

resource "aws_instance" "api" {
  ami           = var.ami_id
  subnet_id     = var.subnet_id
  instance_type = var.api_instance_type # variable: el parcheo automático no aplica

  tags = {
    Name        = "api-prod-1"
    Environment = "production"
  }
}

resource "aws_instance" "batch" {
  ami           = var.ami_id
  subnet_id     = var.subnet_id
  instance_type = "c5.4xlarge"

  tags = {
    Name        = "batch-dev-1"
    Environment = "development"
  }
}

resource "aws_instance" "reports" {
  ami           = var.ami_id
  subnet_id     = var.subnet_id
  instance_type = "r5.xlarge"

  tags = {
    Name        = "reports-stg-1"
    Environment = "staging"
  }
}

# --------------------------------------------------------------- Almacenamiento
resource "aws_ebs_volume" "api_data" {
  availability_zone = "us-east-1a"
  size              = 100
  type              = "gp3"

  tags = {
    Name        = "api-data-prod"
    Environment = "production"
  }
}

resource "aws_volume_attachment" "api_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.api_data.id
  instance_id = aws_instance.api.id
}

resource "aws_ebs_volume" "legacy_data" {
  availability_zone = "us-east-1a"
  size              = 500
  type              = "gp2"

  tags = {
    Name        = "legacy-data-prod"
    Environment = "production"
  }
}

resource "aws_ebs_volume" "scratch" {
  availability_zone = "us-east-1b"
  size              = 200
  type              = "gp3"

  tags = {
    Name        = "scratch-dev"
    Environment = "development"
  }
}

resource "aws_ebs_snapshot" "old_backup" {
  volume_id = aws_ebs_volume.api_data.id

  tags = {
    Name        = "old-backup-dev"
    Environment = "development"
  }
}

resource "aws_ebs_snapshot" "legal_hold" {
  volume_id = aws_ebs_volume.api_data.id

  tags = {
    Name        = "audit-2021-snapshot"
    Environment = "production"
    retain      = "true" # protegido: ninguna regla lo propone
  }
}

# --------------------------------------------------------------- Bases de datos
# Tras la migración a la nueva región, el clúster anterior quedó encendido y sin conexiones.
# Declaran final_snapshot_identifier: sin esa red de seguridad la plataforma se niega a proponer su eliminación.
resource "aws_db_instance" "orders_legacy_writer" {
  identifier                = "orders-legacy-writer"
  engine                    = "aurora-postgresql"
  instance_class            = "db.r5.2xlarge"
  allocated_storage         = 500
  skip_final_snapshot       = false
  final_snapshot_identifier = "orders-legacy-writer-final"

  tags = {
    Name        = "orders-legacy-writer"
    Environment = "production"
  }
}

resource "aws_db_instance" "orders_legacy_reader" {
  identifier                = "orders-legacy-reader"
  engine                    = "aurora-postgresql"
  instance_class            = "db.r5.xlarge"
  allocated_storage         = 500
  skip_final_snapshot       = false
  final_snapshot_identifier = "orders-legacy-reader-final"

  tags = {
    Name        = "orders-legacy-reader"
    Environment = "production"
  }
}

resource "aws_db_instance" "reports_dev" {
  identifier        = "reports-pg-dev"
  engine            = "postgres"
  instance_class    = "db.m5.large"
  allocated_storage = 100
  # sin final_snapshot_identifier: la propuesta se genera, pero el PR no se puede armar hasta añadirlo

  tags = {
    Name        = "reports-pg-dev"
    Environment = "development"
  }
}

resource "aws_db_instance" "billing" {
  identifier        = "billing-prod"
  engine            = "postgres"
  instance_class    = "db.r5.2xlarge"
  allocated_storage = 800
  multi_az          = true

  tags = {
    Name        = "billing-prod"
    Environment = "production"
  }
}

# --------------------------------------------------------------- Restos de un despliegue blue/green fallido
# Cuatro intentos de checkout-v2 dejaron un volumen de datos cada uno (ya sin instancia).
resource "aws_ebs_volume" "rollout_data_1" {
  availability_zone = "us-east-1a"
  size              = 500
  type              = "gp3"

  tags = {
    Name        = "checkout-v2-rollout-data-1"
    Environment = "staging"
  }
}

resource "aws_ebs_volume" "rollout_data_2" {
  availability_zone = "us-east-1a"
  size              = 500
  type              = "gp3"

  tags = {
    Name        = "checkout-v2-rollout-data-2"
    Environment = "staging"
  }
}

resource "aws_ebs_volume" "rollout_data_3" {
  availability_zone = "us-east-1b"
  size              = 500
  type              = "gp3"

  tags = {
    Name        = "checkout-v2-rollout-data-3"
    Environment = "staging"
  }
}

resource "aws_ebs_volume" "rollout_data_4" {
  availability_zone = "us-east-1b"
  size              = 1000
  type              = "gp3"

  tags = {
    Name        = "checkout-v2-rollout-data-4"
    Environment = "staging"
  }
}
# Los dos volúmenes de PVC huérfanos (pvc-feature-checkout-ab-data, pvc-load-test-q3-data) los creó el driver CSI de
# Kubernetes: no existen en Terraform, así que la propuesta indica que se limpian desde el clúster.
