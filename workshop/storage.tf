locals {
  asset_directory = "${path.module}/.tools/source/frontend/static"
  content_types = {
    jpg   = "image/jpeg"
    jpeg  = "image/jpeg"
    png   = "image/png"
    svg   = "image/svg+xml"
    webp  = "image/webp"
    ico   = "image/x-icon"
    txt   = "text/plain; charset=utf-8"
    css   = "text/css"
    js    = "application/javascript"
    html  = "text/html; charset=utf-8"
    woff2 = "font/woff2"
  }
}

resource "aws_cloudformation_stack" "assets" {
  name         = "workshop-assets"
  iam_role_arn = var.lab_role_arn
  template_body = jsonencode({
    AWSTemplateFormatVersion = "2010-09-09"
    Resources = {
      AssetsBucket = {
        Type = "AWS::S3::Bucket"
        Properties = {
          BucketName = var.assets_bucket_name
          Tags = [
            { Key = "Project", Value = "workshop" },
            { Key = "ManagedBy", Value = "Terraform" }
          ]
        }
      }
    }
    Outputs = {
      BucketName         = { Value = { Ref = "AssetsBucket" } }
      RegionalDomainName = { Value = { "Fn::GetAtt" = ["AssetsBucket", "RegionalDomainName"] } }
    }
  })
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_cloudformation_stack.assets.outputs["BucketName"]
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "assets" {
  bucket = aws_cloudformation_stack.assets.outputs["BucketName"]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "arn:aws:s3:::${var.assets_bucket_name}/*"
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.assets]
}

resource "aws_s3_object" "assets" {
  for_each      = fileset(local.asset_directory, "**")
  bucket        = aws_cloudformation_stack.assets.outputs["BucketName"]
  key           = each.key
  source        = "${local.asset_directory}/${each.key}"
  source_hash   = filemd5("${local.asset_directory}/${each.key}")
  content_type  = lookup(local.content_types, lower(element(reverse(split(".", each.key)), 0)), "application/octet-stream")
  cache_control = "public, max-age=3600"
}
