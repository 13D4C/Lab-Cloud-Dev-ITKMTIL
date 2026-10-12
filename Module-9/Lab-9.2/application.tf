resource "aws_s3_object" "application" {
  bucket        = var.site_bucket
  key           = "lab92/application-${filemd5("${path.module}/.tools/application.zip")}.zip"
  source        = "${path.module}/.tools/application.zip"
  etag          = filemd5("${path.module}/.tools/application.zip")
  content_type  = "application/zip"
  cache_control = "max-age=180"
}

resource "aws_elastic_beanstalk_application_version" "application" {
  count       = var.create_application_version ? 1 : 0
  name        = "Lab92-${filemd5("${path.module}/.tools/application.zip")}"
  application = "MyNodeApp"
  bucket      = aws_s3_object.application.bucket
  key         = aws_s3_object.application.key
}
