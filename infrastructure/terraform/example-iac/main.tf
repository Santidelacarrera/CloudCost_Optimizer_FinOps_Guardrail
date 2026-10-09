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

# --------------------------------------------------------------- Despliegues fallidos (los volúmenes pvc-* los creó Kubernetes: no tienen bloque aquí)
resource "aws_ebs_volume" "tmp_rollout" {
  availability_zone = "us-east-1b"
  size              = 250
  type              = "gp2"

  tags = {
    Name        = "tmp-rollout-aug-failed"
    Environment = "staging"
  }
}

resource "aws_ebs_volume" "canary" {
  availability_zone = "us-east-1a"
  size              = 300
  type              = "gp3"

  tags = {
    Name        = "canary-sep-failed"
    Environment = "production"
  }
}

# --------------------------------------------------------------- Bases de datos
resource "aws_db_instance" "orders_legacy" {
  identifier          = "orders-legacy-dev"
  engine              = "mysql"
  instance_class      = "db.m5.large"
  allocated_storage   = 200
  skip_final_snapshot = true

  tags = {
    Name        = "orders-legacy-dev"
    Environment = "development"
  }
}

resource "aws_db_instance" "reports_stg_old" {
  identifier          = "reports-stg-old"
  engine              = "postgres"
  instance_class      = "db.r5.large"
  allocated_storage   = 500
  multi_az            = true
  skip_final_snapshot = true

  tags = {
    Name        = "reports-stg-old"
    Environment = "staging"
  }
}

# Referencia a la base anterior: el parche automático se niega a eliminarla mientras alguien la use (caso «referenced»).
resource "aws_ssm_parameter" "reports_db_host" {
  name  = "/reports/db_host"
  type  = "String"
  value = aws_db_instance.reports_stg_old.address
}

resource "aws_db_instance" "legacy_crm" {
  identifier          = "legacy-crm-prod"
  engine              = "postgres"
  instance_class      = "db.t3.large"
  allocated_storage   = 100
  deletion_protection = true
  skip_final_snapshot = false

  tags = {
    Name        = "legacy-crm-prod"
    Environment = "production"
  }
}

resource "aws_db_instance" "customers" {
  identifier        = "customers-prod"
  engine            = "postgres"
  instance_class    = "db.r5.xlarge"
  allocated_storage = 800
  multi_az          = true

  tags = {
    Name        = "customers-prod"
    Environment = "production"
  }
}
