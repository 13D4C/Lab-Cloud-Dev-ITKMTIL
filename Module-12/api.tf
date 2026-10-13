resource "aws_api_gateway_authorizer" "cafe" {
  name            = "cafe_lockdown"
  rest_api_id     = var.api_id
  type            = "COGNITO_USER_POOLS"
  provider_arns   = [aws_cognito_user_pool.cafe.arn]
  identity_source = "method.request.header.Authorization"
}

resource "aws_api_gateway_method" "report" {
  rest_api_id          = var.api_id
  resource_id          = var.report_resource_id
  http_method          = "POST"
  authorization        = "COGNITO_USER_POOLS"
  authorizer_id        = aws_api_gateway_authorizer.cafe.id
  api_key_required     = var.report_method.api_key_required
  request_models       = var.report_method.request_models
  request_validator_id = var.report_method.request_validator_id
  request_parameters = merge(var.report_method.request_parameters, {
    "method.request.header.Authorization" = true
  })
}

resource "aws_api_gateway_integration_response" "report" {
  for_each           = var.report_responses
  rest_api_id        = var.api_id
  resource_id        = var.report_resource_id
  http_method        = each.key
  status_code        = "200"
  selection_pattern  = each.value.selection_pattern
  response_templates = each.value.response_templates
  response_parameters = merge(each.value.response_parameters, {
    "method.response.header.Access-Control-Allow-Origin"  = "'${local.website_url}'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
  })
  depends_on = [aws_api_gateway_method.report]
}

resource "aws_api_gateway_resource" "beans" {
  rest_api_id = var.api_id
  parent_id   = var.api_root_id
  path_part   = "bean_products"
}

resource "aws_api_gateway_method" "beans" {
  rest_api_id   = var.api_id
  resource_id   = aws_api_gateway_resource.beans.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "beans" {
  rest_api_id             = var.api_id
  resource_id             = aws_api_gateway_resource.beans.id
  http_method             = aws_api_gateway_method.beans.http_method
  integration_http_method = "GET"
  type                    = "HTTP_PROXY"
  uri                     = "${var.application_url}/beans.json"
}

resource "aws_api_gateway_method" "beans_options" {
  rest_api_id   = var.api_id
  resource_id   = aws_api_gateway_resource.beans.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "beans_options" {
  rest_api_id       = var.api_id
  resource_id       = aws_api_gateway_resource.beans.id
  http_method       = aws_api_gateway_method.beans_options.http_method
  type              = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\":200}" }
}

resource "aws_api_gateway_method_response" "beans_options" {
  rest_api_id = var.api_id
  resource_id = aws_api_gateway_resource.beans.id
  http_method = aws_api_gateway_method.beans_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "beans_options" {
  rest_api_id = var.api_id
  resource_id = aws_api_gateway_resource.beans.id
  http_method = aws_api_gateway_method.beans_options.http_method
  status_code = aws_api_gateway_method_response.beans_options.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'${local.website_url}'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,Authorization'"
  }
  depends_on = [aws_api_gateway_integration.beans_options]
}

resource "aws_api_gateway_deployment" "cafe" {
  rest_api_id = var.api_id
  triggers = {
    configuration = sha1(jsonencode([
      aws_api_gateway_authorizer.cafe.id,
      aws_api_gateway_method.report.request_parameters,
      { for verb, response in aws_api_gateway_integration_response.report : verb => response.response_parameters },
      aws_api_gateway_integration.beans.uri,
      aws_api_gateway_integration_response.beans_options.response_parameters
    ]))
  }
  depends_on = [aws_api_gateway_integration.beans_options, aws_api_gateway_integration_response.report, aws_api_gateway_integration_response.beans_options]
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = var.api_id
  stage_name    = "prod"
  deployment_id = aws_api_gateway_deployment.cafe.id
  lifecycle {
    ignore_changes = [access_log_settings, cache_cluster_enabled, cache_cluster_size, client_certificate_id, description, documentation_version, tags, variables, xray_tracing_enabled]
  }
}
