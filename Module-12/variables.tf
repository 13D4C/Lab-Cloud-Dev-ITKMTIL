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

variable "bucket_statements" {
  type = any
}

variable "api_id" {
  type = string
}

variable "api_root_id" {
  type = string
}

variable "report_resource_id" {
  type = string
}

variable "cloudfront_domain" {
  type = string
}

variable "application_url" {
  type = string
}

variable "client_cidr" {
  type = string
  validation {
    condition     = can(cidrhost(var.client_cidr, 0)) && endswith(var.client_cidr, "/32")
    error_message = "CLIENT_CIDR must be your public IPv4 address followed by /32."
  }
}

variable "report_email" {
  type = string
}

variable "frank_password" {
  type      = string
  sensitive = true
}

variable "report_method" {
  type = any
}

variable "report_responses" {
  type = any
}
