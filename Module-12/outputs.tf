output "website_url" {
  value = local.website_url
}

output "login_url" {
  value = local.login_url
}

output "create_report_url" {
  value = local.report_url
}

output "user_pool_id" {
  value = aws_cognito_user_pool.cafe.id
}

output "client_id" {
  value = aws_cognito_user_pool_client.cafe.id
}

output "deployment_id" {
  value = aws_api_gateway_deployment.cafe.id
}

output "cloudfront_acl_arn" {
  value = aws_wafv2_web_acl.office["global"].arn
}

output "username" {
  value = aws_cognito_user.frank.username
}
