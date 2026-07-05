data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  bucket     = "${var.project_name}-${local.account_id}"
  secret_arn = "arn:aws:secretsmanager:*:${local.account_id}:secret:${var.youtube_secret_name}*"
}

# --- Networking for the Fargate worker (default VPC unless overridden) -------

data "aws_vpc" "default" {
  count   = var.vpc_id == null ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = length(var.subnet_ids) == 0 ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }
}

locals {
  vpc_id     = var.vpc_id != null ? var.vpc_id : data.aws_vpc.default[0].id
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : data.aws_subnets.default[0].ids
}

resource "aws_security_group" "worker" {
  name        = "${var.project_name}-worker"
  description = "Egress-only for the ffmpeg worker"
  vpc_id      = local.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Pipeline bucket ----------------------------------------------------------

resource "aws_s3_bucket" "pipeline" {
  bucket = local.bucket
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket                  = aws_s3_bucket.pipeline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id

  rule {
    id     = "expire-work-artifacts"
    status = "Enabled"
    filter {
      prefix = "work/"
    }
    expiration {
      days = var.work_expiration_days
    }
  }
}

# EventBridge gets every Object Created event; the rule filters incoming/.
resource "aws_s3_bucket_notification" "pipeline" {
  bucket      = aws_s3_bucket.pipeline.id
  eventbridge = true
}

# --- Jobs table -----------------------------------------------------------------

resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project_name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }
}

# --- Worker image registry -------------------------------------------------------

resource "aws_ecr_repository" "worker" {
  name         = "${var.project_name}-worker"
  force_delete = true
}
