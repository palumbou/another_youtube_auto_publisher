# Video uploaded under incoming/ -> start the analysis pipeline.
resource "aws_cloudwatch_event_rule" "video_uploaded" {
  name        = "${var.project_name}-video-uploaded"
  description = "S3 Object Created under incoming/ starts the pipeline"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.pipeline.bucket] }
      object = { key = [{ prefix = "incoming/" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "start_pipeline" {
  rule     = aws_cloudwatch_event_rule.video_uploaded.name
  arn      = aws_sfn_state_machine.pipeline.arn
  role_arn = aws_iam_role.events.arn
}

# Daily publisher: one Short per firing.
resource "aws_cloudwatch_event_rule" "publish_schedule" {
  name                = "${var.project_name}-publish"
  schedule_expression = var.publish_schedule
}

resource "aws_cloudwatch_event_target" "publish" {
  rule = aws_cloudwatch_event_rule.publish_schedule.name
  arn  = aws_lambda_function.publish.arn
}

resource "aws_lambda_permission" "publish_schedule" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.publish.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.publish_schedule.arn
}
