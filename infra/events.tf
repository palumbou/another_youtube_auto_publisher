# READY marker written under incoming/ -> SQS -> ingest Lambda.
resource "aws_cloudwatch_event_rule" "ready_marker" {
  name        = "${var.project_name}-ready-marker"
  description = "S3 Object Created for a READY marker under incoming/ is queued for ingestion"
  tags        = local.tags

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.pipeline.bucket] }
      object = { key = [{ prefix = "incoming/" }, { suffix = "/READY" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "queue_ready" {
  rule = aws_cloudwatch_event_rule.ready_marker.name
  arn  = aws_sqs_queue.ingest.arn
}

resource "aws_lambda_event_source_mapping" "ingest" {
  event_source_arn                   = aws_sqs_queue.ingest.arn
  function_name                      = aws_lambda_function.ingest.arn
  batch_size                         = 5
  maximum_batching_window_in_seconds = 10
  function_response_types            = ["ReportBatchItemFailures"]
}

# Durable scheduler for the publish worker (disabled by default).
resource "aws_scheduler_schedule_group" "publish" {
  name = "${var.project_name}-publish"
  tags = local.tags
}

resource "aws_scheduler_schedule" "publish" {
  name                         = "${var.project_name}-publish-due"
  group_name                   = aws_scheduler_schedule_group.publish.name
  schedule_expression          = var.publish_schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = var.publish_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.publish.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ action = "publish" })
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
