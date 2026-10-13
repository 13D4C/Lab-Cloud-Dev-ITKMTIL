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

resource "aws_api_gateway_method" "cors" {
  rest_api_id   = var.api_id
  resource_id   = aws_api_gateway_resource.beans.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "cors" {
  rest_api_id       = var.api_id
  resource_id       = aws_api_gateway_resource.beans.id
  http_method       = aws_api_gateway_method.cors.http_method
  type              = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\":200}" }
}

resource "aws_api_gateway_method_response" "cors" {
  rest_api_id = var.api_id
  resource_id = aws_api_gateway_resource.beans.id
  http_method = aws_api_gateway_method.cors.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "cors" {
  rest_api_id = var.api_id
  resource_id = aws_api_gateway_resource.beans.id
  http_method = aws_api_gateway_method.cors.http_method
  status_code = aws_api_gateway_method_response.cors.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
  }
  depends_on = [aws_api_gateway_integration.cors]
}

resource "aws_api_gateway_resource" "report" {
  rest_api_id = var.api_id
  parent_id   = var.api_root_id
  path_part   = "create_report"
}

resource "aws_api_gateway_method" "report" {
  rest_api_id   = var.api_id
  resource_id   = aws_api_gateway_resource.report.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "report" {
  rest_api_id             = var.api_id
  resource_id             = aws_api_gateway_resource.report.id
  http_method             = aws_api_gateway_method.report.http_method
  integration_http_method = "POST"
  type                    = "AWS"
  uri                     = "arn:${data.aws_partition.current.partition}:apigateway:${var.aws_region}:states:action/StartExecution"
  credentials             = var.api_execution_role_arn
  passthrough_behavior    = "WHEN_NO_TEMPLATES"
  request_templates = {
    "application/json" = <<-VTL
      #set($payload = $input.body)
      #if($payload == "")
      #set($payload = "{}")
      #end
      {
        "input": "$util.escapeJavaScript($payload).replaceAll("\\'", "'")",
        "stateMachineArn": "${aws_sfn_state_machine.report.arn}"
      }
    VTL
  }
}

resource "aws_api_gateway_method_response" "report" {
  rest_api_id = var.api_id
  resource_id = aws_api_gateway_resource.report.id
  http_method = aws_api_gateway_method.report.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "report" {
  rest_api_id = var.api_id
  resource_id = aws_api_gateway_resource.report.id
  http_method = aws_api_gateway_method.report.http_method
  status_code = aws_api_gateway_method_response.report.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
  }
  depends_on = [aws_api_gateway_integration.report]
}

resource "aws_api_gateway_deployment" "application" {
  rest_api_id = var.api_id
  triggers = {
    configuration = sha1(jsonencode([
      aws_api_gateway_integration.beans.uri,
      aws_api_gateway_integration.beans.id,
      aws_api_gateway_integration_response.cors.response_parameters,
      aws_api_gateway_integration.report.uri,
      aws_api_gateway_integration.report.credentials,
      aws_api_gateway_integration.report.request_templates,
      aws_api_gateway_integration_response.report.response_parameters
    ]))
  }
  lifecycle {
    create_before_destroy = true
  }
  depends_on = [aws_api_gateway_integration_response.cors, aws_api_gateway_integration_response.report]
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = var.api_id
  deployment_id = aws_api_gateway_deployment.application.id
  stage_name    = "prod"
}

