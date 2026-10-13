output "website_url" {
  value = "https://${var.site_bucket}.s3.${var.aws_region}.amazonaws.com/index.html"
}

output "site_bucket" {
  value = var.site_bucket
}

output "allowed_client_cidr" {
  value = var.client_cidr
}

output "lab_ide_url" {
  value = var.lab_ide_url
}
