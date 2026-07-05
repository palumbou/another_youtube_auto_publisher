output "bucket" {
  value       = aws_s3_bucket.pipeline.bucket
  description = "Upload videos to s3://<bucket>/incoming/"
}

output "jobs_table" {
  value = aws_dynamodb_table.jobs.name
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "worker_repository_url" {
  value = aws_ecr_repository.worker.repository_url
}

output "webui_url" {
  description = "Review UI — open this link once, a cookie takes over"
  value       = "${aws_lambda_function_url.webui.function_url}?token=${random_password.webui_token.result}"
  sensitive   = true
}

output "worker_image_push" {
  description = "Build and push the worker image"
  value       = <<-EOT
    aws ecr get-login-password | docker login --username AWS --password-stdin ${aws_ecr_repository.worker.repository_url}
    docker build -t ${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag} ../worker
    docker push ${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}
  EOT
}
