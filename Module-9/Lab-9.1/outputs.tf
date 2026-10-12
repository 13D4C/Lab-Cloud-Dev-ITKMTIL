output "cache_endpoint" {
  value = aws_elasticache_cluster.cache.cluster_address
}

output "cache_nodes" {
  value = aws_elasticache_cluster.cache.cache_nodes
}

output "application_version" {
  value      = "Lab91-${filemd5("${path.module}/.tools/application.zip")}"
  depends_on = [aws_elastic_beanstalk_application_version.application]
}

output "application_url" {
  value = var.application_url
}

output "beans_api_url" {
  value = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod/bean_products"
}

output "cafe_url" {
  value = "http://${var.site_bucket}.s3-website-${var.aws_region}.amazonaws.com/index.html"
}
