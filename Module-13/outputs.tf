output "repository_name" {
  value = aws_codecommit_repository.website.repository_name
}

output "repository_clone_url" {
  value = "codecommit::${var.aws_region}://${aws_codecommit_repository.website.repository_name}"
}

output "pipeline_name" {
  value = try(aws_codepipeline.website[0].name, null)
}

output "beans_api_url" {
  value = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod/bean_products"
}
