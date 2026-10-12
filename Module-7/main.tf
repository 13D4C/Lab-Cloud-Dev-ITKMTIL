provider "aws" {
  region = var.aws_region
}

provider "aws" {
  alias  = "website"
  region = var.website_bucket_region
}

locals {
  endpoints = {
    "/products" = {
      method   = "get"
      function = "get_all_products"
      request  = jsonencode({})
    }
    "/products/on_offer" = {
      method   = "get"
      function = "get_all_products"
      request  = jsonencode({ path = "on_offer" })
    }
    "/create_report" = {
      method   = "post"
      function = "create_report"
      request  = jsonencode({})
    }
  }
  cors_headers = {
    "Access-Control-Allow-Headers" = { schema = { type = "string" } }
    "Access-Control-Allow-Origin"  = { schema = { type = "string" } }
    "Access-Control-Allow-Methods" = { schema = { type = "string" } }
  }
  cors_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
  preflight = {
    security = []
    responses = {
      "200" = {
        description = "Successful response"
        headers     = local.cors_headers
      }
    }
    "x-amazon-apigateway-integration" = {
      type                = "mock"
      passthroughBehavior = "when_no_match"
      requestTemplates    = { "application/json" = "{\"statusCode\": 200}" }
      responses = {
        default = {
          statusCode = "200"
          responseParameters = merge(local.cors_parameters, {
            "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
          })
          responseTemplates = { "application/json" = "{}" }
        }
      }
    }
  }
  openapi = {
    openapi = "3.0.1"
    info = {
      title       = "ProductsApi"
      description = "API to get all the food products."
      version     = "1.0.10"
    }
    components = {
      schemas = { Empty = { type = "object" } }
    }
    "x-amazon-apigateway-gateway-responses" = {
      for response_type in ["DEFAULT_4XX", "DEFAULT_5XX"] : response_type => {
        responseParameters = {
          "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
          "gatewayresponse.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
          "gatewayresponse.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
        }
      }
    }
    paths = {
      for path, endpoint in local.endpoints : path => merge({
        (endpoint.method) = {
          security = []
          responses = {
            "200" = {
              description = "Successful response"
              headers     = local.cors_headers
              content = {
                "application/json" = { schema = { "$ref" = "#/components/schemas/Empty" } }
              }
            }
          }
          "x-amazon-apigateway-integration" = {
            type                = "aws"
            httpMethod          = "POST"
            uri                 = aws_lambda_function.functions[endpoint.function].invoke_arn
            passthroughBehavior = "when_no_match"
            requestTemplates    = { "application/json" = endpoint.request }
            responses = {
              default = {
                statusCode = "200"
                responseParameters = merge(local.cors_parameters, {
                  "method.response.header.Access-Control-Allow-Methods" = "'${upper(endpoint.method)}'"
                })
                responseTemplates = { "application/json" = "$input.json('$')" }
              }
            }
          }
        }
      }, endpoint.method == "get" ? { options = local.preflight } : {})
    }
  }
  api_base_url = "https://${aws_api_gateway_rest_api.products.id}.execute-api.${var.aws_region}.amazonaws.com/prod"
  website_files = setsubtract(
    fileset("${path.module}/website", "**"),
    toset(["config.js"])
  )
  content_types = {
    html = "text/html; charset=utf-8"
    css  = "text/css; charset=utf-8"
    js   = "application/javascript"
    json = "application/json"
    png  = "image/png"
    jpeg = "image/jpeg"
    jpg  = "image/jpeg"
    ico  = "image/x-icon"
  }
}

resource "aws_api_gateway_rest_api" "products" {
  name                     = "ProductsApi"
  description              = "API to get all the food products."
  minimum_compression_size = "123"
  put_rest_api_mode        = "merge"
  body                     = jsonencode(local.openapi)

  lifecycle {
    ignore_changes = [tags]
  }

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_deployment" "products" {
  depends_on = [aws_lambda_permission.gateway]

  rest_api_id = aws_api_gateway_rest_api.products.id

  triggers = {
    redeployment = sha1(aws_api_gateway_rest_api.products.body)
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.products.id
  deployment_id = aws_api_gateway_deployment.products.id
  stage_name    = "prod"

  lifecycle {
    ignore_changes = [tags]
  }
}

resource "aws_s3_bucket_policy" "website" {
  provider = aws.website
  bucket   = var.website_bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CafeWebsiteReadByIp"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:aws:s3:::${var.website_bucket}/*"
        Condition = { IpAddress = { "aws:SourceIp" = [var.website_source_cidr] } }
      },
      {
        Sid       = "DenyOneObjectIfRequestNotSigned"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:aws:s3:::${var.website_bucket}/report.html"
        Condition = { StringNotEquals = { "s3:authtype" = "REST-QUERY-STRING" } }
      }
    ]
  })
}

resource "aws_s3_object" "website" {
  provider = aws.website
  for_each = local.website_files

  bucket        = var.website_bucket
  key           = each.key
  source        = "${path.module}/website/${each.key}"
  source_hash   = filesha256("${path.module}/website/${each.key}")
  content_type  = lookup(local.content_types, lower(reverse(split(".", each.key))[0]), "application/octet-stream")
  cache_control = "max-age=0"
}

resource "aws_s3_object" "config" {
  provider = aws.website
  bucket   = var.website_bucket
  key      = "config.js"
  content = "window.COFFEE_CONFIG = ${jsonencode({
    API_GW_BASE_URL_STR        = local.api_base_url
    COGNITO_LOGIN_BASE_URL_STR = null
  })};\n"
  content_type  = "application/javascript"
  cache_control = "max-age=0"

  depends_on = [aws_api_gateway_stage.prod]
}
