resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-pipeline"
  role_arn = aws_iam_role.sfn.arn

  definition = templatefile("${path.module}/statemachine.asl.json", {
    pipeline_lambda_arn = aws_lambda_function.pipeline.arn
    analyze_lambda_arn  = aws_lambda_function.analyze.arn
    cluster_arn         = aws_ecs_cluster.main.arn
    task_definition_arn = aws_ecs_task_definition.worker.arn
    subnets             = jsonencode(local.subnet_ids)
    security_group      = aws_security_group.worker.id
    bucket              = aws_s3_bucket.pipeline.bucket
  })
}
