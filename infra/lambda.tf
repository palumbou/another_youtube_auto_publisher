# One zip with the pure-Python package; boto3 comes from the Lambda runtime.
data "archive_file" "package" {
  type        = "zip"
  output_path = "${path.module}/build/autopublisher.zip"

  dynamic "source" {
    for_each = fileset("${path.module}/../autopublisher", "*.py")
    content {
      filename = "autopublisher/${source.value}"
      content  = file("${path.module}/../autopublisher/${source.value}")
    }
  }
}

locals {
  lambda_env = {
    BUCKET     = aws_s3_bucket.pipeline.bucket
    JOBS_TABLE = aws_dynamodb_table.jobs.name
  }
}

resource "aws_lambda_function" "pipeline" {
  function_name    = "${var.project_name}-pipeline"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "autopublisher.pipeline.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = local.lambda_env
  }
}

resource "aws_lambda_function" "analyze" {
  function_name    = "${var.project_name}-analyze"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "autopublisher.analyze.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  timeout          = 600
  memory_size      = 1024

  environment {
    variables = merge(local.lambda_env, {
      MODEL_ID   = var.model_id
      MIN_SHORTS = tostring(var.min_shorts)
      MAX_SHORTS = tostring(var.max_shorts)
    })
  }
}

resource "aws_lambda_function" "publish" {
  function_name    = "${var.project_name}-publish"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "autopublisher.publish.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  timeout          = 900 # streams whole videos to YouTube
  memory_size      = 1024

  environment {
    variables = merge(local.lambda_env, {
      YOUTUBE_SECRET = var.youtube_secret_name
    })
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = {
    pipeline = aws_lambda_function.pipeline.function_name
    analyze  = aws_lambda_function.analyze.function_name
    publish  = aws_lambda_function.publish.function_name
    webui    = aws_lambda_function.webui.function_name
  }
  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
}
