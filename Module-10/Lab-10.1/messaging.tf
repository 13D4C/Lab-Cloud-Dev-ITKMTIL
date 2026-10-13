locals {
  root_arn  = "arn:${data.aws_partition.current.partition}:iam::${var.account_id}:root"
  dlq_arn   = "arn:${data.aws_partition.current.partition}:sqs:${var.aws_region}:${var.account_id}:DeadLetterQueue.fifo"
  queue_arn = "arn:${data.aws_partition.current.partition}:sqs:${var.aws_region}:${var.account_id}:updated_beans.fifo"
  topic_arn = "arn:${data.aws_partition.current.partition}:sns:${var.aws_region}:${var.account_id}:updated_beans_sns.fifo"
}

resource "aws_sqs_queue" "dead_letter" {
  name                        = "DeadLetterQueue.fifo"
  fifo_queue                  = true
  visibility_timeout_seconds  = 20
  receive_wait_time_seconds   = 0
  message_retention_seconds   = 345600
  content_based_deduplication = false
  deduplication_scope         = "queue"
  fifo_throughput_limit       = "perQueue"
  policy = jsonencode({
    Version = "2008-10-17"
    Id      = "DlqSqsPolicy"
    Statement = [{
      Sid       = "dead-letter-sqs"
      Effect    = "Allow"
      Principal = { AWS = local.root_arn }
      Action    = ["SQS:*"]
      Resource  = local.dlq_arn
    }]
  })
}

resource "aws_sqs_queue" "beans" {
  name                        = "updated_beans.fifo"
  fifo_queue                  = true
  visibility_timeout_seconds  = 30
  receive_wait_time_seconds   = 20
  message_retention_seconds   = 345600
  content_based_deduplication = true
  deduplication_scope         = "queue"
  fifo_throughput_limit       = "perQueue"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter.arn
    maxReceiveCount     = 5
  })
  policy = jsonencode({
    Version = "2008-10-17"
    Id      = "BeansSqsPolicy"
    Statement = [
      {
        Sid       = "beans-sqs"
        Effect    = "Allow"
        Principal = { AWS = local.root_arn }
        Action    = "SQS:*"
        Resource  = local.queue_arn
      },
      {
        Sid       = "topic-subscription"
        Effect    = "Allow"
        Principal = { AWS = local.root_arn, Service = "sns.amazonaws.com" }
        Action    = "SQS:SendMessage"
        Resource  = local.queue_arn
        Condition = { ArnLike = { "aws:SourceArn" = local.topic_arn } }
      },
      {
        Sid       = "get-messages"
        Effect    = "Allow"
        Principal = { AWS = [var.consumer_role_arn, local.root_arn] }
        Action    = ["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:ReceiveMessage"]
        Resource  = local.queue_arn
      }
    ]
  })
}

resource "aws_sns_topic" "beans" {
  name                        = "updated_beans_sns.fifo"
  display_name                = "updated beans sns"
  fifo_topic                  = true
  content_based_deduplication = true
  policy = jsonencode({
    Version = "2008-10-17"
    Id      = "BeansTopicPolicy"
    Statement = [
      {
        Sid       = "BeansAllowActions"
        Effect    = "Allow"
        Principal = { AWS = local.root_arn }
        Action = [
          "SNS:Publish", "SNS:RemovePermission", "SNS:SetTopicAttributes", "SNS:DeleteTopic",
          "SNS:ListSubscriptionsByTopic", "SNS:GetTopicAttributes", "SNS:Receive", "SNS:AddPermission", "SNS:Subscribe"
        ]
        Resource  = local.topic_arn
        Condition = { StringEquals = { "AWS:SourceOwner" = var.account_id } }
      },
      {
        Sid       = "BeansAllowPublish"
        Effect    = "Allow"
        Principal = { AWS = local.root_arn }
        Action    = "SNS:Publish"
        Resource  = local.topic_arn
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "beans" {
  topic_arn            = aws_sns_topic.beans.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.beans.arn
  raw_message_delivery = false
  depends_on           = [aws_sqs_queue.beans]
}
