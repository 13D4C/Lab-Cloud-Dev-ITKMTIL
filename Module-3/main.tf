locals {
  website_root  = "${path.module}/.tools/source/resources/website"
  content_types = jsondecode(file("${path.module}/mime-types.json"))
  bucket_arn    = "arn:aws:s3:::${var.site_bucket}"
}

resource "terraform_data" "website" {
  input = {
    bucket  = var.site_bucket
    account = var.account_id
    region  = var.aws_region
    stack   = var.stack_name
  }
  triggers_replace = [var.site_bucket, var.account_id, var.aws_region]
  provisioner "local-exec" {
    interpreter = [var.python_executable]
    command     = "${path.module}/bucket.py"
    environment = {
      LAB_SITE_BUCKET = var.site_bucket
      LAB_ACCOUNT_ID  = var.account_id
    }
  }
}

resource "aws_s3_bucket_public_access_block" "website" {
  bucket                  = var.site_bucket
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = true
  depends_on              = [terraform_data.website]
}

resource "aws_s3_bucket_policy" "website" {
  bucket = var.site_bucket
  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = ["${local.bucket_arn}/*", local.bucket_arn]
        Condition = {
          IpAddress = {
            "aws:SourceIp" = [var.client_cidr]
          }
        }
      },
      {
        Sid       = "DenyOneObjectIfRequestNotSigned"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${local.bucket_arn}/report.html"
        Condition = {
          StringNotEquals = {
            "s3:authtype" = "REST-QUERY-STRING"
          }
        }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.website]
}

resource "aws_s3_object" "website" {
  for_each      = fileset(local.website_root, "**")
  bucket        = var.site_bucket
  key           = each.value
  source        = "${local.website_root}/${each.value}"
  etag          = filemd5("${local.website_root}/${each.value}")
  content_type  = lookup(local.content_types, lower(reverse(split(".", each.value))[0]), "application/octet-stream")
  cache_control = "max-age=0"
  depends_on    = [terraform_data.website]
}
