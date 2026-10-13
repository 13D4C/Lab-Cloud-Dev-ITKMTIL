resource "aws_lambda_function" "presigned_url" {
  function_name    = "GeneratePresignedURL"
  role             = var.lambda_role_arn
  runtime          = "python3.9"
  handler          = "presigned_url.lambda_handler"
  filename         = "${path.module}/.tools/presigned_url.zip"
  source_code_hash = filebase64sha256("${path.module}/.tools/presigned_url.zip")
  timeout          = 30
  environment {
    variables = {
      REPORT_BUCKET = var.site_bucket
      REPORT_EXPIRY = "120"
    }
  }
}

resource "aws_lambda_function" "generate_html" {
  function_name    = "generateHTML"
  role             = var.lambda_role_arn
  runtime          = "nodejs20.x"
  handler          = "generate_html.handler"
  filename         = "${path.module}/.tools/report_functions.zip"
  source_code_hash = filebase64sha256("${path.module}/.tools/report_functions.zip")
  timeout          = 120
  environment {
    variables = {
      REPORT_BUCKET = var.site_bucket
      REPORT_EXPIRY = "120"
    }
  }
}

resource "aws_lambda_function" "report_data" {
  function_name    = "generateReportData"
  role             = var.lambda_role_arn
  runtime          = "nodejs20.x"
  handler          = "report_data.handler"
  filename         = "${path.module}/.tools/report_functions.zip"
  source_code_hash = filebase64sha256("${path.module}/.tools/report_functions.zip")
  timeout          = 60
  environment {
    variables = {
      DB_HOST = var.database_endpoint
    }
  }
  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.database_security_group_id]
  }
}
