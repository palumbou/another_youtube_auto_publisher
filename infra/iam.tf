locals {
  table_arns  = [for t in aws_dynamodb_table.state : t.arn]
  bucket_arns = [aws_s3_bucket.pipeline.arn, "${aws_s3_bucket.pipeline.arn}/*"]
  lambda_assume = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  state_rw = {
    Sid      = "State"
    Effect   = "Allow"
    Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
    Resource = local.table_arns
  }
}

# --- Ingest Lambda: reads the job prefix, writes state, starts executions --------------

resource "aws_iam_role" "ingest" {
  name               = "${var.project_name}-ingest"
  assume_role_policy = local.lambda_assume
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ingest_logs" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ingest" {
  name = "ingest-access"
  role = aws_iam_role.ingest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.state_rw,
      {
        Sid      = "ReadIncoming"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"]
        Resource = local.bucket_arns
      },
      {
        Sid      = "Queue"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.ingest.arn
      },
      {
        Sid      = "StartProcessing"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = aws_sfn_state_machine.processing.arn
      },
    ]
  })
}

# --- Console Lambda: state and short-lived read links only; no secrets ---------------------

resource "aws_iam_role" "console" {
  name               = "${var.project_name}-console"
  assume_role_policy = local.lambda_assume
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "console_logs" {
  role       = aws_iam_role.console.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "console" {
  name = "console-access"
  role = aws_iam_role.console.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.state_rw,
      {
        Sid      = "SignedReads"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["${aws_s3_bucket.pipeline.arn}/incoming/*", "${aws_s3_bucket.pipeline.arn}/ready/*", "${aws_s3_bucket.pipeline.arn}/work/*"]
      },
      {
        Sid      = "Reprocess"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = aws_sfn_state_machine.processing.arn
      },
    ]
  })
}

# --- Publish Lambda: the only principal that can read the YouTube secret -------------------

resource "aws_iam_role" "publish" {
  name               = "${var.project_name}-publish"
  assume_role_policy = local.lambda_assume
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "publish_logs" {
  role       = aws_iam_role.publish.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "publish" {
  name = "publish-access"
  role = aws_iam_role.publish.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.state_rw,
      {
        Sid      = "ReadApprovedAssets"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["${aws_s3_bucket.pipeline.arn}/incoming/*", "${aws_s3_bucket.pipeline.arn}/ready/*"]
      },
      {
        Sid      = "YouTubeSecret"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.youtube.arn
      },
    ]
  })
}

# --- Fargate worker roles ---------------------------------------------------------------

resource "aws_iam_role" "worker_execution" {
  name = "${var.project_name}-worker-execution"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "worker_execution" {
  role       = aws_iam_role.worker_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "worker_task" {
  name = "${var.project_name}-worker-task"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "worker_task" {
  name = "worker-access"
  role = aws_iam_role.worker_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.state_rw,
      {
        Sid      = "Media"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:ListBucket"]
        Resource = local.bucket_arns
      },
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${local.account_id}:inference-profile/*",
        ]
      },
      {
        Sid      = "Transcribe"
        Effect   = "Allow"
        Action   = ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"]
        Resource = "*"
      },
    ]
  })
}

# --- Step Functions role ------------------------------------------------------------------

resource "aws_iam_role" "sfn" {
  name = "${var.project_name}-processing"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sfn" {
  name = "processing-orchestration"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunWorker"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = [
          aws_ecs_task_definition.worker.arn_without_revision,
          "${aws_ecs_task_definition.worker.arn_without_revision}:*",
        ]
      },
      {
        Sid      = "TrackWorker"
        Effect   = "Allow"
        Action   = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource = "*"
      },
      {
        Sid      = "PassWorkerRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.worker_task.arn, aws_iam_role.worker_execution.arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Sid      = "SyncRule"
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = "arn:aws:events:*:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
      },
      {
        Sid      = "RecordFailure"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.ingest.arn, "${aws_lambda_function.ingest.arn}:*"]
      },
    ]
  })
}

# --- EventBridge Scheduler -> publish Lambda -----------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name = "${var.project_name}-scheduler"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-publish"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.publish.arn, "${aws_lambda_function.publish.arn}:*"]
    }]
  })
}
