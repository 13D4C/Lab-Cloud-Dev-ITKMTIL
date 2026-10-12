data "aws_iam_role" "lambda" {
  name = var.lambda_role_name
}

locals {
  functions = toset(["get_all_products", "create_report"])
}

resource "aws_s3_object" "lambda_code" {
  provider = aws.website
  for_each = local.functions

  bucket       = var.website_bucket
  key          = "${each.key}_code.zip"
  source       = "${path.module}/.tools/${each.key}_code.zip"
  source_hash  = filesha256("${path.module}/.tools/${each.key}_code.zip")
  content_type = "application/zip"
}

resource "aws_lambda_function" "functions" {
  for_each = local.functions

  function_name    = each.key
  role             = data.aws_iam_role.lambda.arn
  runtime          = var.lambda_runtime
  handler          = "${each.key}_code.lambda_handler"
  s3_bucket        = aws_s3_object.lambda_code[each.key].bucket
  s3_key           = aws_s3_object.lambda_code[each.key].key
  source_code_hash = filebase64sha256("${path.module}/.tools/${each.key}_code.zip")
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.food_products.name
      INDEX_NAME = "special_GSI"
    }
  }
}

resource "aws_lambda_permission" "gateway" {
  for_each = local.endpoints

  statement_id  = "AllowGateway${upper(each.value.method)}${replace(replace(each.key, "/", "-"), "_", "-")}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions[each.value.function].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.products.execution_arn}/*/${upper(each.value.method)}${each.key}"
}
