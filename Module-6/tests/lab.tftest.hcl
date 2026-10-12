mock_provider "aws" {}

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
      one(aws_api_gateway_rest_api.products.endpoint_configuration).types == tolist(["REGIONAL"])
    )
    error_message = "The lab requires a regional ProductsApi deployed to prod."
  }

  assert {
    condition = (
      length(jsondecode(local.endpoints["/products"].body).product_item_arr) == 3 &&
      length(jsondecode(local.endpoints["/products/on_offer"].body).product_item_arr) == 1 &&
      jsondecode(local.endpoints["/create_report"].body).msg_str == "report requested, check your phone shortly"
    )
    error_message = "All three mock responses must match the original lab."
  }

  assert {
    condition = (
      local.openapi.paths["/products"].get["x-amazon-apigateway-integration"].type == "mock" &&
      local.openapi.paths["/products/on_offer"].get["x-amazon-apigateway-integration"].type == "mock" &&
      local.openapi.paths["/create_report"].post["x-amazon-apigateway-integration"].type == "mock" &&
      can(local.openapi.paths["/products"].options) &&
      can(local.openapi.paths["/products/on_offer"].options) &&
      !can(local.openapi.paths["/create_report"].options)
    )
    error_message = "The mock routes and GET preflight responses must match this lab."
  }

  assert {
    condition = (
      contains(local.website_files, "index.html") &&
      contains(local.website_files, "scripts/pastries.js") &&
      !contains(local.website_files, "config.js") &&
      jsondecode(aws_s3_bucket_policy.website.policy).Statement[0].Condition.IpAddress["aws:SourceIp"][0] == "203.0.113.10/32" &&
      jsondecode(aws_s3_bucket_policy.website.policy).Statement[1].Effect == "Deny"
    )
    error_message = "The website must reuse the supplied bucket, restrict reads to the client IP and protect report.html."
  }
}
