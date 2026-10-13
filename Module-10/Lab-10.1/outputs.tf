output "dead_letter_queue_url" {
  value = aws_sqs_queue.dead_letter.url
}

output "queue_url" {
  value = aws_sqs_queue.beans.url
}

output "queue_arn" {
  value = aws_sqs_queue.beans.arn
}

output "topic_arn" {
  value = aws_sns_topic.beans.arn
}

output "subscription_arn" {
  value = aws_sns_topic_subscription.beans.arn
}

output "application_url" {
  value = "${var.application_url}/beans"
}

output "application_version" {
  value      = "Lab101-${filemd5("${path.module}/.tools/application.zip")}"
  depends_on = [aws_elastic_beanstalk_application_version.application]
}

output "website_url" {
  value = var.website_url
}

output "beans_api_url" {
  value = "https://${var.api_id}.execute-api.${var.aws_region}.amazonaws.com/prod/bean_products"
}
