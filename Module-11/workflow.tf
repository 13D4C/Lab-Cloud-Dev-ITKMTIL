resource "aws_cloudwatch_log_group" "state_machine" {
  name              = "/aws/vendedlogs/states/MyStateMachine-Logs"
  retention_in_days = 7
}

resource "aws_sfn_state_machine" "report" {
  name     = "MyStateMachine"
  type     = "EXPRESS"
  role_arn = var.state_machine_role_arn
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
  definition = <<-ASL
{
  "QueryLanguage": "JSONata",
  "StartAt": "getRealData",
  "States": {
    "Process Report": {
      "Branches": [
        {
          "StartAt": "generateHTML",
          "States": {
            "generateHTML": {
              "Arguments": {
                "FunctionName": "${aws_lambda_function.generate_html.arn}:$LATEST",
                "Payload": "{% $states.input %}"
              },
              "End": true,
              "Output": "{% $states.result.Payload %}",
              "Resource": "arn:${data.aws_partition.current.partition}:states:::lambda:invoke",
              "Type": "Task"
            }
          }
        },
        {
          "StartAt": "GeneratePresignedURL",
          "States": {
            "GeneratePresignedURL": {
              "Arguments": {
                "FunctionName": "${aws_lambda_function.presigned_url.arn}:$LATEST",
                "Payload": {}
              },
              "End": true,
              "Output": "{% $states.result.Payload %}",
              "Resource": "arn:${data.aws_partition.current.partition}:states:::lambda:invoke",
              "Type": "Task"
            }
          }
        }
      ],
      "Next": "SNS Publish",
      "Type": "Parallel"
    },
    "SNS Publish": {
      "Arguments": {
        "Message": "{% $string($states.input) %}",
        "TopicArn": "${aws_sns_topic.email_report.arn}"
      },
      "End": true,
      "Output": "{% $states.input %}",
      "Resource": "arn:${data.aws_partition.current.partition}:states:::sns:publish",
      "Type": "Task"
    },
    "getRealData": {
      "Arguments": {
        "FunctionName": "${aws_lambda_function.report_data.arn}:$LATEST",
        "Payload": {}
      },
      "Next": "Process Report",
      "Output": "{% $states.result.Payload.body %}",
      "Resource": "arn:${data.aws_partition.current.partition}:states:::lambda:invoke",
      "Retry": [
        {
          "BackoffRate": 2,
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds": 2,
          "MaxAttempts": 3
        }
      ],
      "Type": "Task"
    }
  }
}
  ASL
}
