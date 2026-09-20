output "bucket" {
  value       = aws_s3_bucket.pipeline.bucket
  description = "Producers upload jobs under s3://<bucket>/incoming/<project>/<job_id>/ and write READY last"
}

output "cutover_at" {
  value       = var.cutover_at
  description = "Only READY objects created at or after this instant are eligible"
}

output "console_url" {
  value       = aws_apigatewayv2_api.console.api_endpoint
  description = "Review console (Cognito login, WAF IP allowlist)"
}

output "cognito_hosted_ui" {
  value = "https://${aws_cognito_user_pool_domain.console.domain}.auth.${data.aws_region.current.region}.amazoncognito.com/login?client_id=${aws_cognito_user_pool_client.console.id}&response_type=code&scope=openid+email&redirect_uri=${aws_apigatewayv2_api.console.api_endpoint}/"
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.processing.arn
}

output "ingest_queue_url" {
  value = aws_sqs_queue.ingest.id
}

output "ingest_dlq_url" {
  value = aws_sqs_queue.ingest_dlq.id
}

output "youtube_secret_arn" {
  value       = aws_secretsmanager_secret.youtube.arn
  description = "Add the OAuth JSON here after scripts/authorize.py; IaC never writes the value"
}

output "worker_repository_url" {
  value = aws_ecr_repository.worker.repository_url
}

output "worker_image_push" {
  description = "Build and push the worker image from the repository root"
  value       = <<-EOT
    aws ecr get-login-password | docker login --username AWS --password-stdin ${aws_ecr_repository.worker.repository_url}
    docker build -f worker/Dockerfile -t ${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag} .
    docker push ${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}
  EOT
}
