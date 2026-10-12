mock_provider "aws" {
  mock_data "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/LambdaAccessToDynamoDB"
    }
  }
}

mock_provider "aws" {
  alias = "website"
}

variables {
  website_bucket      = "lab-cafe-s3bucket"
  website_source_cidr = "203.0.113.10/32"
}

run "lab_final_state" {
  command = plan

  assert {
    condition = (
      aws_api_gateway_rest_api.products.name == "ProductsApi" &&
      aws_api_gateway_stage.prod.stage_name == "prod" &&
      length(aws_lambda_function.functions) == 2 &&
      aws_lambda_function.functions["get_all_products"].handler == "get_all_products_code.lambda_handler" &&
      aws_lambda_function.functions["create_report"].runtime == "python3.12"
    )
    error_message = "The lab requires two Python Lambda functions connected to ProductsApi in prod."
  }

  assert {
    condition = (
      alltrue([for path, endpoint in local.endpoints : local.openapi.paths[path][endpoint.method]["x-amazon-apigateway-integration"].type == "aws"]) &&
      jsondecode(local.openapi.paths["/products/on_offer"].get["x-amazon-apigateway-integration"].requestTemplates["application/json"]).path == "on_offer" &&
      jsondecode(local.openapi.paths["/products"].get["x-amazon-apigateway-integration"].requestTemplates["application/json"]) == {} &&
      length(aws_lambda_permission.gateway) == 3
    )
    error_message = "All three routes must invoke Lambda and only on_offer must send its path."
  }

  assert {
    condition = (
      local.openapi.paths["/products"].options["x-amazon-apigateway-integration"].type == "mock" &&
      local.openapi.paths["/products/on_offer"].options["x-amazon-apigateway-integration"].type == "mock" &&
      local.openapi["x-amazon-apigateway-gateway-responses"].DEFAULT_4XX.responseParameters["gatewayresponse.header.Access-Control-Allow-Origin"] == "'*'" &&
      local.openapi["x-amazon-apigateway-gateway-responses"].DEFAULT_5XX.responseParameters["gatewayresponse.header.Access-Control-Allow-Origin"] == "'*'"
    )
    error_message = "GET preflight and gateway error responses must include CORS."
  }

  assert {
    condition = (
      length(aws_dynamodb_table_item.products) == 26 &&
      jsondecode(aws_dynamodb_table_item.products["apple pie slice"].item).product_id.S == "a444" &&
      contains(local.website_files, "index.html") &&
      jsondecode(aws_s3_bucket_policy.website.policy).Statement[0].Condition.IpAddress["aws:SourceIp"][0] == "203.0.113.10/32"
    )
    error_message = "The foundation must seed the original menu and publish the website to the lab bucket with IP-restricted access."
  }
}
