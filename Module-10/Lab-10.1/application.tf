resource "aws_s3_bucket_policy" "website" {
  bucket = var.site_bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(var.preserved_bucket_statements, [{
      Sid       = "AllowOAIAccess"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::cloudfront:user/CloudFront Origin Access Identity ${var.origin_access_identity_id}" }
      Action    = "s3:GetObject"
      Resource  = "arn:${data.aws_partition.current.partition}:s3:::${var.site_bucket}/*"
    }])
  })
}

resource "aws_s3_object" "application" {
  bucket       = var.site_bucket
  key          = "lab101/application-${filemd5("${path.module}/.tools/application.zip")}.zip"
  source       = "${path.module}/.tools/application.zip"
  etag         = filemd5("${path.module}/.tools/application.zip")
  content_type = "application/zip"
}

resource "aws_elastic_beanstalk_application_version" "application" {
  count       = var.create_application_version ? 1 : 0
  name        = "Lab101-${filemd5("${path.module}/.tools/application.zip")}"
  application = "MyNodeApp"
  bucket      = aws_s3_object.application.bucket
  key         = aws_s3_object.application.key
}
