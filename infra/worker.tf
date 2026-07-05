resource "aws_ecs_cluster" "main" {
  name = var.project_name
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.project_name}-worker"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project_name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.worker_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  ephemeral_storage {
    size_in_gib = var.worker_ephemeral_gb
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # MODE / JOB_ID / VIDEO_KEY are injected per run by the state machine.
  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
      essential = true
      environment = [
        { name = "BUCKET", value = aws_s3_bucket.pipeline.bucket }
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
