locals {
  website_url = "https://${var.cloudfront_domain}"
  api_url     = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod"
  report_url  = "${local.api_url}/create_report"
  login_url = "https://${aws_cognito_user_pool_domain.cafe.domain}.auth.${var.aws_region}.amazoncognito.com/login?${join("&", [
    "client_id=${aws_cognito_user_pool_client.cafe.id}",
    "response_type=token",
    "scope=email+openid",
    "redirect_uri=${urlencode("${local.website_url}/callback.html")}"
  ])}"
  website_config = "window.COFFEE_CONFIG = ${jsonencode({
    API_GW_BASE_URL_STR        = local.api_url
    COGNITO_LOGIN_BASE_URL_STR = local.login_url
  })};\n"
}

resource "aws_cognito_user_pool" "cafe" {
  name                     = "the_cafe"
  mfa_configuration        = "OFF"
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  username_configuration {
    case_sensitive = false
  }

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  schema {
    name                     = "email"
    attribute_data_type      = "String"
    developer_only_attribute = false
    mutable                  = true
    required                 = true
    string_attribute_constraints {
      min_length = "0"
      max_length = "2048"
    }
  }
}

resource "aws_cognito_user_pool_client" "cafe" {
  name                                 = "the_cafe_app_client"
  user_pool_id                         = aws_cognito_user_pool.cafe.id
  generate_secret                      = false
  explicit_auth_flows                  = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["implicit"]
  allowed_oauth_scopes                 = ["email", "openid"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.website_url}/callback.html"]
  logout_urls                          = ["${local.website_url}/sign_out.html"]
  default_redirect_uri                 = "${local.website_url}/callback.html"
  prevent_user_existence_errors        = "ENABLED"
}

resource "aws_cognito_user_pool_domain" "cafe" {
  domain                = "the-cafe-${var.account_id}"
  user_pool_id          = aws_cognito_user_pool.cafe.id
  managed_login_version = 1
}

resource "aws_cognito_resource_server" "cafe" {
  name         = "cafe_resource_server"
  identifier   = local.report_url
  user_pool_id = aws_cognito_user_pool.cafe.id
}

resource "aws_cognito_user" "frank" {
  user_pool_id   = aws_cognito_user_pool.cafe.id
  username       = "frank"
  password       = var.frank_password
  message_action = "SUPPRESS"
  attributes = {
    email = var.report_email
  }
}

resource "aws_s3_object" "config" {
  bucket        = var.site_bucket
  key           = "config.js"
  content_type  = "application/javascript"
  cache_control = "max-age=0"
  content       = local.website_config
  etag          = md5(local.website_config)
}

resource "aws_s3_object" "callback" {
  bucket        = var.site_bucket
  key           = "callback.html"
  content_type  = "text/html"
  cache_control = "max-age=0"
  content       = "<!doctype html><html><head><meta charset=\"utf-8\"><title>Signing in</title></head><body><p>Signing in...</p><script>const p=new URLSearchParams(location.hash.slice(1));const t=p.get('id_token');if(t){localStorage.setItem('bearer_str',t);location.replace('/index.html');}else{document.querySelector('p').textContent=p.get('error_description')||'Sign-in failed. Please return to the website and try again.';}</script></body></html>"
}

resource "aws_s3_object" "sign_out" {
  bucket        = var.site_bucket
  key           = "sign_out.html"
  content_type  = "text/html"
  cache_control = "max-age=0"
  content       = "<!doctype html><html><head><meta charset=\"utf-8\"><title>Signing out</title></head><body><script>localStorage.removeItem('bearer_str');location.replace('/index.html');</script></body></html>"
}
