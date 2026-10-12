resource "aws_cloudfront_origin_access_identity" "website" {
  comment = "access-identity-cafe-website"
}

resource "aws_cloudfront_origin_access_control" "website" {
  name                              = "access-identity-cafe-website"
  description                       = "CloudFront access to the cafe website"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_function" "random_image" {
  name    = "random_image_header"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = file("${path.module}/random_image_header.js")
}

resource "aws_cloudfront_distribution" "website" {
  enabled             = true
  is_ipv6_enabled     = false
  default_root_object = "index.html"
  comment             = "access-identity-cafe-website"
  price_class         = "PriceClass_All"
  web_acl_id          = aws_wafv2_web_acl.website.arn
  wait_for_deployment = true

  origin {
    domain_name = "${var.site_bucket}.s3.amazonaws.com"
    origin_id   = var.site_bucket
    s3_origin_config {
      origin_access_identity = aws_cloudfront_origin_access_identity.website.cloudfront_access_identity_path
    }
    custom_header {
      name  = "cf"
      value = "1"
    }
  }

  default_cache_behavior {
    target_origin_id       = var.site_bucket
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    min_ttl                = 0
    default_ttl            = 86400
    max_ttl                = 31536000
    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }
    function_association {
      event_type   = "viewer-response"
      function_arn = aws_cloudfront_function.random_image.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name = "access-identity-cafe-website"
  }
}

resource "aws_s3_bucket_policy" "website" {
  bucket = var.site_bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(var.preserved_bucket_statements, [{
      Sid       = "Lab92CloudFrontReadOnly"
      Effect    = "Allow"
      Principal = { AWS = aws_cloudfront_origin_access_identity.website.iam_arn }
      Action    = "s3:GetObject"
      Resource  = "arn:aws:s3:::${var.site_bucket}/*"
    }])
  })
  depends_on = [aws_cloudfront_distribution.website]
}
