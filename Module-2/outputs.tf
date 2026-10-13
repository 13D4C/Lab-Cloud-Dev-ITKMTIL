output "sample_bucket" {
  value = var.sample_bucket
}

output "hello_world_url" {
  value = "https://${var.sample_bucket}.s3.${var.aws_region}.amazonaws.com/index.html"
}

output "lab_ide_url" {
  value = var.lab_ide_url
}

output "python_script" {
  value = "s3://${var.sample_bucket}/${aws_s3_object.list_buckets.key}"
}
