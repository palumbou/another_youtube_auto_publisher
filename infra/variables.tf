variable "project_name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "autopublisher"
}

variable "environment" {
  description = "Deployment environment name; LOCAL_MODE is never enabled here."
  type        = string
  default     = "prod"
  validation {
    condition     = var.environment != "local"
    error_message = "The cloud stack must not be deployed as the local environment."
  }
}

variable "cutover_at" {
  description = "Immutable RFC 3339 UTC instant: only READY objects created at or after it are eligible. Changing it is an audited administrative change recorded in the deployment report."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.cutover_at))
    error_message = "cutover_at must look like 2026-09-20T00:00:00Z."
  }
}

variable "allowed_projects" {
  description = "Project keys accepted under incoming/."
  type        = list(string)
  default     = ["quiz-al-volo"]
}

variable "review_ip_allowlist" {
  description = "IPv4/IPv6 CIDRs allowed to reach the review console (WAF IP set and application check). Empty means nobody can reach it."
  type        = list(string)
  validation {
    condition     = length(var.review_ip_allowlist) > 0
    error_message = "At least one CIDR is required; the console is default-deny."
  }
}

variable "owner_email" {
  description = "Cognito user (the single reviewer), alarm and budget notifications."
  type        = string
}

variable "owner_timezone" {
  type    = string
  default = "Europe/Rome"
}

variable "publish_kill_switch" {
  description = "When true the publish Lambda refuses every upload; review stays available."
  type        = bool
  default     = true
}

variable "youtube_mode" {
  description = "fake records requests and uploads nothing; real uses the OAuth refresh token in Secrets Manager."
  type        = string
  default     = "fake"
  validation {
    condition     = contains(["fake", "real"], var.youtube_mode)
    error_message = "youtube_mode must be fake or real."
  }
}

variable "youtube_api_project_audited" {
  description = "Set true only after Google completed the YouTube API compliance audit; until then uploads stay private and no publishAt is requested."
  type        = bool
  default     = false
}

variable "daily_upload_cap" {
  type    = number
  default = 3
}

variable "weekly_upload_cap" {
  type    = number
  default = 10
}

variable "max_shorts_per_job" {
  type    = number
  default = 4
}

variable "model_id" {
  description = "Bedrock model id for the analysis provider (cross-region inference profile prefix matching your region)."
  type        = string
  default     = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "youtube_secret_name" {
  description = "Secrets Manager secret holding the YouTube OAuth JSON (client_id, client_secret, refresh_token). The value is added by the owner after the one-time authorization; never by IaC."
  type        = string
  default     = "youtube-publisher"
}

variable "publish_schedule_expression" {
  description = "EventBridge Scheduler expression for the publish worker; each run uploads only schedules that are due."
  type        = string
  default     = "rate(30 minutes)"
}

variable "publish_schedule_enabled" {
  type    = bool
  default = false
}

variable "vpc_id" {
  type    = string
  default = null
}

variable "subnet_ids" {
  type    = list(string)
  default = []
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
  type    = number
  default = 50
}

variable "worker_image_tag" {
  type    = string
  default = "latest"
}

variable "work_expiration_days" {
  description = "Days after which work/ artifacts (transcripts, plans, logs) expire."
  type        = number
  default     = 30
}

variable "ready_expiration_days" {
  description = "Days after which rendered ready/ assets expire. Approved and uploaded assets are re-derivable from the immutable source."
  type        = number
  default     = 90
}

variable "noncurrent_version_expiration_days" {
  type    = number
  default = 30
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "monthly_budget_usd" {
  description = "AWS Budgets monthly limit for the cost filter on this project's tag."
  type        = number
  default     = 25
}

variable "max_processing_concurrency" {
  description = "Reserved concurrency of the ingest Lambda; bounds parallel Fargate tasks indirectly."
  type        = number
  default     = 2
}
