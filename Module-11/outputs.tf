output "state_machine_arn" {
  value = aws_sfn_state_machine.report.arn
}

output "topic_arn" {
  value = aws_sns_topic.email_report.arn
}

output "subscription_arn" {
  value = try(aws_sns_topic_subscription.email_report[0].arn, null)
}

output "create_report_url" {
  value = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod/create_report"
}

output "website_url" {
  value = "https://${var.site_bucket}.s3.${var.aws_region}.amazonaws.com/index.html"
}

output "application_url" {
  value = "${var.application_url}/beans"
}

output "application_version" {
  value      = "Lab111-${filemd5("${path.module}/.tools/application.zip")}"
  depends_on = [aws_elastic_beanstalk_application_version.application]
}
