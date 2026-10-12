variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "website_bucket" {
  type = string
}

variable "website_bucket_region" {
  type    = string
  default = "us-east-1"
}

variable "website_source_cidr" {
  type = string

  validation {
    condition     = can(cidrnetmask(var.website_source_cidr))
    error_message = "website_source_cidr must be an IPv4 CIDR."
  }
}
