resource "aws_wafv2_ip_set" "office" {
  provider           = aws.global
  name               = "office"
  description        = "office IP"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses          = [var.client_cidr]
}

resource "aws_wafv2_web_acl" "website" {
  provider    = aws.global
  name        = "cafe-website-office-only-during-dev"
  description = "Allow access to the cafe website through CloudFront from the cafe office"
  scope       = "CLOUDFRONT"
  default_action {
    block {}
  }
  rule {
    name     = "only_office_please"
    priority = 0
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.office.arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "only_office_please"
      sampled_requests_enabled   = true
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "cafe-website-office-only-during-dev"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_ip_set" "office_regional" {
  name               = "office_regional"
  description        = "IP of the office for API Gateway"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = [var.client_cidr]
}

resource "aws_wafv2_web_acl" "api" {
  name        = "website-api-gw-office-only-during-dev"
  description = "To allow us to access the API GW calls used by the website from the office"
  scope       = "REGIONAL"
  default_action {
    block {}
  }
  rule {
    name     = "ip_for_apigw"
    priority = 0
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.office_regional.arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "ip_for_apigw"
      sampled_requests_enabled   = true
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "website-api-gw-office-only-during-dev"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = "arn:aws:apigateway:${var.aws_region}::/restapis/${var.api_id}/stages/${aws_api_gateway_stage.prod.stage_name}"
  web_acl_arn  = aws_wafv2_web_acl.api.arn
}
