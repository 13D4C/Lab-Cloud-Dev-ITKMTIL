resource "aws_s3_bucket_policy" "website" {
  bucket = var.site_bucket
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = var.bucket_statements
  })
}
