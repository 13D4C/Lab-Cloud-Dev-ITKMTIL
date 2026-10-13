resource "aws_s3_bucket_policy" "website" {
  bucket = var.site_bucket
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = var.bucket_statements
  })
}

resource "aws_wafv2_ip_set" "office" {
  for_each           = { global = "CLOUDFRONT", regional = "REGIONAL" }
  name               = each.key == "global" ? "office" : "office_regional"
  scope              = each.value
  ip_address_version = "IPV4"
  addresses          = [var.client_cidr]
}

resource "aws_wafv2_web_acl" "office" {
  for_each = { global = "CLOUDFRONT", regional = "REGIONAL" }
  name     = each.key == "global" ? "cafe-website-office-only-during-dev" : "website-api-gw-office-only-during-dev"
  scope    = each.value

  default_action {
    block {}
  }

  rule {
    name     = each.key == "global" ? "only_office_please" : "ip_for_apigw"
    priority = 0
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.office[each.key].arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      sampled_requests_enabled   = true
      metric_name                = each.key == "global" ? "only_office_please" : "ip_for_apigw"
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    sampled_requests_enabled   = true
    metric_name                = each.key == "global" ? "cafe-website-office-only-during-dev" : "website-api-gw-office-only-during-dev"
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = "arn:aws:apigateway:${var.aws_region}::/restapis/${var.api_id}/stages/prod"
  web_acl_arn  = aws_wafv2_web_acl.office["regional"].arn
  depends_on   = [aws_api_gateway_stage.prod]
}
