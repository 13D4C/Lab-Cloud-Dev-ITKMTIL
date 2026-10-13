variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type = string
}

variable "stack_name" {
  type = string
}

variable "site_bucket" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.site_bucket))
    error_message = "SITE_BUCKET must be a valid S3 bucket name without dots."
  }
}

variable "client_cidr" {
  type = string
  validation {
    condition     = can(cidrhost(var.client_cidr, 0)) && endswith(var.client_cidr, "/32") && !startswith(var.client_cidr, "0.0.0.0/")
    error_message = "CLIENT_CIDR must be your public IPv4 address followed by /32."
  }
}

variable "lab_ide_url" {
  type = string
}

variable "python_executable" {
  type = string
}
