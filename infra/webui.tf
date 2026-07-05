resource "random_password" "webui_token" {
  length  = 40
  special = false
}

resource "aws_lambda_function" "webui" {
  function_name    = "${var.project_name}-webui"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "autopublisher.webui.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = merge(local.lambda_env, {
      WEBUI_TOKEN       = random_password.webui_token.result
      STATE_MACHINE_ARN = aws_sfn_state_machine.pipeline.arn
    })
  }
}

resource "aws_lambda_function_url" "webui" {
  function_name      = aws_lambda_function.webui.function_name
  authorization_type = "NONE" # access is gated by the shared token inside the handler
}
