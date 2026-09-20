# One zip with the pure-Python package and the contract schema; boto3 and
# jsonschema come from the runtime and the layer built by scripts/build_layer.sh.
data "archive_file" "package" {
  type        = "zip"
  output_path = "${path.module}/build/autopublisher.zip"

  dynamic "source" {
    for_each = fileset("${path.module}/../autopublisher", "**/*.py")
    content {
      filename = "autopublisher/${source.value}"
      content  = file("${path.module}/../autopublisher/${source.value}")
    }
  }

  dynamic "source" {
    for_each = fileset("${path.module}/../contracts", "**/*.json")
    content {
      filename = "contracts/${source.value}"
      content  = file("${path.module}/../contracts/${source.value}")
    }
  }
}

resource "aws_lambda_layer_version" "deps" {
  layer_name          = "${var.project_name}-deps"
  filename            = "${path.module}/build/deps-layer.zip"
  compatible_runtimes = ["python3.12"]
  description         = "jsonschema and rfc3339-validator, built by scripts/build_layer.sh"
}

locals {
  common_env = {
    ENVIRONMENT                 = var.environment
    BUCKET                      = aws_s3_bucket.pipeline.bucket
    TABLE_PREFIX                = var.project_name
    CUTOVER_AT                  = var.cutover_at
    ALLOWED_PROJECTS            = join(",", var.allowed_projects)
    MANIFEST_REQUIRED_PROJECTS  = join(",", var.allowed_projects)
    OWNER_TIMEZONE              = var.owner_timezone
    PUBLISH_KILL_SWITCH         = var.publish_kill_switch ? "1" : "0"
    YOUTUBE_MODE                = var.youtube_mode
    YOUTUBE_API_PROJECT_AUDITED = var.youtube_api_project_audited ? "1" : "0"
    DAILY_UPLOAD_CAP            = tostring(var.daily_upload_cap)
    WEEKLY_UPLOAD_CAP           = tostring(var.weekly_upload_cap)
    MAX_SHORTS_PER_JOB          = tostring(var.max_shorts_per_job)
    REVIEW_IP_ALLOWLIST         = join(",", var.review_ip_allowlist)
    STATE_MACHINE_ARN           = "arn:aws:states:${data.aws_region.current.region}:${local.account_id}:stateMachine:${var.project_name}-processing"
  }
}

resource "aws_lambda_function" "ingest" {
  function_name                  = "${var.project_name}-ingest"
  role                           = aws_iam_role.ingest.arn
  runtime                        = "python3.12"
  handler                        = "autopublisher.pipeline.handler"
  filename                       = data.archive_file.package.output_path
  source_code_hash               = data.archive_file.package.output_base64sha256
  layers                         = [aws_lambda_layer_version.deps.arn]
  timeout                        = 300
  memory_size                    = 512
  reserved_concurrent_executions = var.max_processing_concurrency
  tags                           = local.tags

  environment {
    variables = merge(local.common_env, { INGEST_PROBE_MEDIA = "0" })
  }
}

resource "aws_lambda_function" "console" {
  function_name    = "${var.project_name}-console"
  role             = aws_iam_role.console.arn
  runtime          = "python3.12"
  handler          = "autopublisher.pipeline.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = 30
  memory_size      = 512
  tags             = local.tags

  environment {
    variables = merge(local.common_env, { CONSOLE_ALLOWED_USERS = var.owner_email })
  }
}

resource "aws_lambda_function" "publish" {
  function_name    = "${var.project_name}-publish"
  role             = aws_iam_role.publish.arn
  runtime          = "python3.12"
  handler          = "autopublisher.pipeline.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = 900
  memory_size      = 1024
  tags             = local.tags

  ephemeral_storage {
    size = 4096
  }

  environment {
    variables = merge(local.common_env, { YOUTUBE_SECRET = var.youtube_secret_name })
  }
}

resource "aws_lambda_permission" "publish_scheduler" {
  statement_id  = "AllowSchedulerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.publish.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_scheduler_schedule.publish.arn
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = {
    ingest  = aws_lambda_function.ingest.function_name
    console = aws_lambda_function.console.function_name
    publish = aws_lambda_function.publish.function_name
  }
  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}
