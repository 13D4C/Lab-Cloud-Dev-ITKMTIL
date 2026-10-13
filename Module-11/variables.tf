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

variable "api_id" {
  type = string
}

variable "api_root_id" {
  type = string
}

variable "application_url" {
  type = string
}

variable "lambda_role_arn" {
  type = string
}

variable "state_machine_role_arn" {
  type = string
}

variable "api_execution_role_arn" {
  type = string
}

variable "database_endpoint" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "database_security_group_id" {
  type = string
}

variable "report_email" {
  type = string
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.report_email))
    error_message = "Set report_email to an email address you can confirm."
  }
}

variable "subscribe_email" {
  type    = bool
  default = true
}

variable "client_cidr" {
  type = string
  validation {
    condition     = can(cidrhost(var.client_cidr, 0)) && endswith(var.client_cidr, "/32")
    error_message = "CLIENT_CIDR must contain your public IPv4 address followed by /32."
  }
}

variable "preserved_bucket_statements" {
  type    = any
  default = []
}

variable "create_application_version" {
  type    = bool
  default = true
}
