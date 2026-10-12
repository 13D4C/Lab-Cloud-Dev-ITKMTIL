output "database_endpoint" {
  value = aws_rds_cluster.database.endpoint
}

output "database_arn" {
  value = aws_rds_cluster.database.arn
}

output "query_editor_secret_arn" {
  value = aws_secretsmanager_secret.query_editor.arn
}

output "application_url" {
  value = "http://${aws_elastic_beanstalk_environment.application.cname}"
}

output "beans_api_url" {
  value = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod/bean_products"
}

output "cafe_url" {
  value = "http://${var.site_bucket}.s3-website-${var.aws_region}.amazonaws.com/index.html"
}

output "repository_url" {
  value = data.aws_ecr_repository.application.repository_url
}
