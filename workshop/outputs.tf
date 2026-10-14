output "website_url" {
  value = "http://${aws_lb.workshop.dns_name}"
}

output "api_url" {
  value = "http://${aws_lb.workshop.dns_name}/api/activities"
}

output "assets_url" {
  value = "https://${aws_cloudformation_stack.assets.outputs["RegionalDomainName"]}"
}

output "assets_bucket" {
  value = aws_cloudformation_stack.assets.outputs["BucketName"]
}

output "database_endpoint" {
  value = aws_db_instance.workshop.endpoint
}

output "autoscaling_group" {
  value = aws_autoscaling_group.workshop.name
}

output "target_groups" {
  value = { frontend = aws_lb_target_group.frontend.arn, backend = aws_lb_target_group.backend.arn }
}

output "vpc_id" {
  value = aws_vpc.workshop.id
}

output "subnet_ids" {
  value = { for name, subnet in aws_subnet.workshop : name => subnet.id }
}
