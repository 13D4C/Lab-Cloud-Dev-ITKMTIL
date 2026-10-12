output "website_url" {
  value = "http://${data.aws_instance.ide.public_ip}:3000"
}

output "repository_url" {
  value = aws_ecr_repository.node_app.repository_url
}

output "ide_instance_id" {
  value = data.aws_instance.ide.id
}

output "mysql_host" {
  value = data.aws_instance.mysql.private_ip
}

output "original_website_url" {
  value = "http://${data.aws_instance.app.public_ip}"
}
