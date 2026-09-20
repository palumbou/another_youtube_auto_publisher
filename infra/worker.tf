resource "aws_ecs_cluster" "main" {
  name = var.project_name
  tags = local.tags

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.project_name}-worker"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project_name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn
  tags                     = local.tags

  ephemeral_storage {
    size_in_gib = var.worker_ephemeral_gb
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # MODE / PROJECT_KEY / JOB_ID / SCOPE / REASON are injected per run by the state machine.
  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
      essential = true
      environment = [
        for k, v in merge(local.common_env, {
          MODEL_ID               = var.model_id
          TRANSCRIPTION_PROVIDER = "transcribe"
          ANALYSIS_PROVIDER      = "bedrock"
          VIDEO_ENCODER          = "libx264"
        }) : { name = k, value = v }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.worker.name
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])
}
