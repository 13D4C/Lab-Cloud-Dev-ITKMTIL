resource "aws_s3_bucket_policy" "website" {
  bucket = var.site_bucket
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(var.preserved_bucket_statements, [{
      Sid       = "Lab111OfficeWebsite"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "arn:${data.aws_partition.current.partition}:s3:::${var.site_bucket}/*"
      Condition = { IpAddress = { "aws:SourceIp" = var.client_cidr } }
    }])
  })
}

resource "aws_s3_object" "application" {
  bucket       = var.site_bucket
  key          = "lab111/application-${filemd5("${path.module}/.tools/application.zip")}.zip"
  source       = "${path.module}/.tools/application.zip"
  etag         = filemd5("${path.module}/.tools/application.zip")
  content_type = "application/zip"
}

resource "aws_elastic_beanstalk_application_version" "application" {
  count       = var.create_application_version ? 1 : 0
  name        = "Lab111-${filemd5("${path.module}/.tools/application.zip")}"
  application = "MyNodeApp"
  bucket      = aws_s3_object.application.bucket
  key         = aws_s3_object.application.key
}
