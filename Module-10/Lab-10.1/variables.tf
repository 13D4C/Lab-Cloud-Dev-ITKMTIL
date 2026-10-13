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

variable "consumer_role_arn" {
  type = string
}

variable "origin_access_identity_id" {
  type = string
}

variable "website_url" {
  type = string
}

variable "create_application_version" {
  type    = bool
  default = true
}

variable "client_cidr" {
  type = string
  validation {
    condition     = can(cidrnetmask(var.client_cidr)) && endswith(var.client_cidr, "/32")
    error_message = "Use your public IPv4 address followed by /32."
  }
}

variable "preserved_bucket_statements" {
  type    = any
  default = []
}
