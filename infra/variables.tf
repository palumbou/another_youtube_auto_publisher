variable "project_name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "autopublisher"
}

variable "model_id" {
  description = "Bedrock model id for the analysis step (a Claude vision model; use the cross-region inference profile prefix matching your region: us./eu./apac.)."
  type        = string
  default     = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "youtube_secret_name" {
  description = "Secrets Manager secret holding the YouTube OAuth JSON (client_id, client_secret, refresh_token). Created manually, see the README."
  type        = string
  default     = "youtube-publisher"
}

variable "min_shorts" {
  type    = number
  default = 2
}

variable "max_shorts" {
  type    = number
  default = 6
}

variable "publish_schedule" {
  description = "EventBridge schedule for the publisher (one Short per firing)."
  type        = string
  default     = "cron(0 15 * * ? *)" # daily 15:00 UTC
}

variable "vpc_id" {
  description = "VPC for the Fargate worker. Defaults to the account's default VPC."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Subnets for the Fargate worker (must reach the internet to pull the image and S3). Defaults to the default-VPC subnets."
  type        = list(string)
  default     = []
}

variable "worker_cpu" {
  type    = number
  default = 2048
}

variable "worker_memory" {
  type    = number
  default = 8192
}

variable "worker_ephemeral_gb" {
  description = "Fargate ephemeral storage (GiB) — must fit the largest source video plus rendered clips."
  type        = number
  default     = 50
}

variable "worker_image_tag" {
  type    = string
  default = "latest"
}

variable "work_expiration_days" {
  description = "Days after which work/ artifacts (mosaics, audio, probe output) expire from S3."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  type    = number
  default = 30
}
