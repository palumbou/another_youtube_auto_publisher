resource "aws_sfn_state_machine" "processing" {
  name     = "${var.project_name}-processing"
  role_arn = aws_iam_role.sfn.arn
  tags     = local.tags

  definition = templatefile("${path.module}/statemachine.asl.json", {
    cluster_arn         = aws_ecs_cluster.main.arn
    task_definition_arn = aws_ecs_task_definition.worker.arn
    subnets             = jsonencode(local.subnet_ids)
    security_group      = aws_security_group.worker.id
    ingest_lambda_arn   = aws_lambda_function.ingest.arn
  })
}
