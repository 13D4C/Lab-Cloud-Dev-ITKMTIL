resource "aws_sns_topic" "email_report" {
  name = "EmailReport"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OwnerAccess"
      Effect    = "Allow"
      Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${var.account_id}:root" }
      Action    = ["SNS:Publish", "SNS:RemovePermission", "SNS:SetTopicAttributes", "SNS:DeleteTopic", "SNS:ListSubscriptionsByTopic", "SNS:GetTopicAttributes", "SNS:AddPermission", "SNS:Subscribe", "SNS:Receive"]
      Resource  = "arn:${data.aws_partition.current.partition}:sns:${var.aws_region}:${var.account_id}:EmailReport"
      }, {
      Sid       = "LabPublishAndSubscribe"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["SNS:Publish", "SNS:Subscribe"]
      Resource  = "arn:${data.aws_partition.current.partition}:sns:${var.aws_region}:${var.account_id}:EmailReport"
    }]
  })
}

resource "aws_sns_topic_subscription" "email_report" {
  count     = var.subscribe_email ? 1 : 0
  topic_arn = aws_sns_topic.email_report.arn
  protocol  = "email"
  endpoint  = var.report_email
}
