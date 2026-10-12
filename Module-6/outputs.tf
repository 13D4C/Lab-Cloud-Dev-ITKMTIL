output "api_id" {
  value = aws_api_gateway_rest_api.products.id
}

output "api_base_url" {
  value = local.api_base_url
}

output "website_bucket" {
  value = var.website_bucket
}

output "website_url" {
  value = "https://${var.website_bucket}.s3.${var.website_bucket_region}.amazonaws.com/index.html"
}
