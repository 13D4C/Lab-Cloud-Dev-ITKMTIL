variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "cache_security_group_id" {
  type = string
}

variable "ide_security_group_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "Use at least two subnets from the Lab IDE VPC."
  }
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

variable "create_application_version" {
  type    = bool
  default = true
}
