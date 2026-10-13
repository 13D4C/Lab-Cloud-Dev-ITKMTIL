resource "aws_s3_bucket_ownership_controls" "sample" {
  bucket = var.sample_bucket
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "sample" {
  bucket                  = var.sample_bucket
  block_public_acls       = false
  ignore_public_acls      = false
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_object" "list_buckets" {
  bucket        = var.sample_bucket
  key           = "list-buckets.py"
  source        = "${path.module}/list-buckets.py"
  etag          = filemd5("${path.module}/list-buckets.py")
  content_type  = "text/x-python"
  cache_control = "max-age=0"
}

resource "aws_s3_object" "hello_world" {
  bucket        = var.sample_bucket
  key           = "index.html"
  source        = "${path.module}/index.html"
  etag          = filemd5("${path.module}/index.html")
  content_type  = "text/html"
  cache_control = "max-age=0"
  acl           = "public-read"
  depends_on = [
    aws_s3_bucket_ownership_controls.sample,
    aws_s3_bucket_public_access_block.sample
  ]
}
