# --- Identity: one Cognito user (the owner), MFA optional-on, no self sign-up --------------

resource "aws_cognito_user_pool" "owner" {
  name = "${var.project_name}-owner"
  tags = local.tags

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  mfa_configuration = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]
}

resource "aws_cognito_user" "owner" {
  user_pool_id = aws_cognito_user_pool.owner.id
  username     = var.owner_email
  attributes = {
    email          = var.owner_email
    email_verified = "true"
  }
}

resource "aws_cognito_user_pool_client" "console" {
  name                                 = "${var.project_name}-console"
  user_pool_id                         = aws_cognito_user_pool.owner.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${aws_apigatewayv2_api.console.api_endpoint}/"]
  logout_urls                          = ["${aws_apigatewayv2_api.console.api_endpoint}/login"]
  access_token_validity                = 1
  id_token_validity                    = 1
  refresh_token_validity               = 1
  prevent_user_existence_errors        = "ENABLED"

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "console" {
  domain       = "${var.project_name}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.owner.id
}

# --- HTTP API with a JWT authorizer: the Lambda only ever sees verified claims -----------------

resource "aws_apigatewayv2_api" "console" {
  name          = "${var.project_name}-console"
  protocol_type = "HTTP"
  tags          = local.tags
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.console.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.console.id]
    issuer   = "https://${aws_cognito_user_pool.owner.endpoint}"
  }
}

resource "aws_apigatewayv2_integration" "console" {
  api_id                 = aws_apigatewayv2_api.console.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.console.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "console" {
  api_id             = aws_apigatewayv2_api.console.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.console.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "console" {
  api_id      = aws_apigatewayv2_api.console.id
  name        = "$default"
  auto_deploy = true
  tags        = local.tags

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp", method = "$context.httpMethod",
      path      = "$context.path", status = "$context.status", user = "$context.authorizer.claims.sub"
    })
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/apigateway/${var.project_name}-console"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_lambda_permission" "console_api" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.console.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.console.execution_arn}/*/*"
}

# --- WAF: IP allowlist, default deny, on the API stage ------------------------------------------

resource "aws_wafv2_ip_set" "owner_v4" {
  name               = "${var.project_name}-owner-v4"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = [for c in var.review_ip_allowlist : c if !can(regex(":", c))]
  tags               = local.tags
}

resource "aws_wafv2_ip_set" "owner_v6" {
  name               = "${var.project_name}-owner-v6"
  scope              = "REGIONAL"
  ip_address_version = "IPV6"
  addresses          = [for c in var.review_ip_allowlist : c if can(regex(":", c))]
  tags               = local.tags
}

resource "aws_wafv2_web_acl" "console" {
  name  = "${var.project_name}-console"
  scope = "REGIONAL"
  tags  = local.tags

  default_action {
    block {}
  }

  rule {
    name     = "allow-owner-v4"
    priority = 1
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.owner_v4.arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "allow-owner-v4"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "allow-owner-v6"
    priority = 2
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.owner_v6.arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "allow-owner-v6"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-console"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "console" {
  resource_arn = aws_apigatewayv2_stage.console.arn
  web_acl_arn  = aws_wafv2_web_acl.console.arn
}
