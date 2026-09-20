data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  bucket     = "${var.project_name}-${local.account_id}"
  secret_arn = "arn:aws:secretsmanager:*:${local.account_id}:secret:${var.youtube_secret_name}*"
  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
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
  tags        = local.tags

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Pipeline bucket: private, encrypted, versioned ---------------------------

resource "aws_s3_bucket" "pipeline" {
  bucket = local.bucket
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket                  = aws_s3_bucket.pipeline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "pipeline" {
  bucket     = aws_s3_bucket.pipeline.id
  depends_on = [aws_s3_bucket_versioning.pipeline]

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

  rule {
    id     = "expire-ready-assets"
    status = "Enabled"
    filter {
      prefix = "ready/"
    }
    expiration {
      days = var.ready_expiration_days
    }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

resource "aws_s3_bucket_policy" "pipeline_tls_only" {
  bucket = aws_s3_bucket.pipeline.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.pipeline.arn, "${aws_s3_bucket.pipeline.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# EventBridge gets every Object Created event; the rule filters READY markers.
resource "aws_s3_bucket_notification" "pipeline" {
  bucket      = aws_s3_bucket.pipeline.id
  eventbridge = true
}

# --- State tables --------------------------------------------------------------

locals {
  tables = {
    jobs         = { hash = "pk", range = null }
    revisions    = { hash = "pk", range = "number" }
    schedules    = { hash = "schedule_id", range = null }
    publications = { hash = "publication_id", range = null }
    audit        = { hash = "pk", range = "sequence" }
  }
}

resource "aws_dynamodb_table" "state" {
  for_each     = local.tables
  name         = "${var.project_name}-${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value.hash
  range_key    = each.value.range
  tags         = local.tags

  attribute {
    name = each.value.hash
    type = "S"
  }

  dynamic "attribute" {
    for_each = each.value.range == null ? [] : [each.value.range]
    content {
      name = attribute.value
      type = "N"
    }
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# --- Ingestion queue with dead-letter queue ---------------------------------------

resource "aws_sqs_queue" "ingest_dlq" {
  name                      = "${var.project_name}-ingest-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = local.tags
}

resource "aws_sqs_queue" "ingest" {
  name                       = "${var.project_name}-ingest"
  visibility_timeout_seconds = 360
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  tags                       = local.tags
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingest_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.ingest.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.ready_marker.arn } }
    }]
  })
}

# --- Worker image registry ---------------------------------------------------------

resource "aws_ecr_repository" "worker" {
  name                 = "${var.project_name}-worker"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  tags                 = local.tags

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last five images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# --- Secrets (containers only; values are added by the owner, never by IaC) ---------

resource "aws_secretsmanager_secret" "youtube" {
  name                    = var.youtube_secret_name
  description             = "YouTube OAuth client and refresh token, added after the one-time owner authorization"
  recovery_window_in_days = 7
  tags                    = local.tags
}
