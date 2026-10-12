output "website_url" {
  value = "https://${aws_cloudfront_distribution.website.domain_name}"
}

output "distribution_id" {
  value = aws_cloudfront_distribution.website.id
}

output "origin_access_identity_id" {
  value = aws_cloudfront_origin_access_identity.website.id
}

output "function_name" {
  value = aws_cloudfront_function.random_image.name
}

output "application_url" {
  value = var.application_url
}

output "application_version" {
  value      = "Lab92-${filemd5("${path.module}/.tools/application.zip")}"
  depends_on = [aws_elastic_beanstalk_application_version.application]
}

output "beans_api_url" {
  value = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod/bean_products"
}

output "office_cidr" {
  value = var.client_cidr
}

output "origin_access_control_id" {
  value = aws_cloudfront_origin_access_control.website.id
}
