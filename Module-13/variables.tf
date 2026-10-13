variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type = string
}

variable "site_bucket" {
  type = string
}

variable "artifact_bucket" {
  type = string
}

variable "pipeline_role_arn" {
  type = string
}

variable "enable_pipeline" {
  type    = bool
  default = true
}

variable "api_id" {
  type = string
}

variable "api_root_id" {
  type = string
}

variable "application_url" {
  type = string
}

variable "bucket_statements" {
  type = any
}
