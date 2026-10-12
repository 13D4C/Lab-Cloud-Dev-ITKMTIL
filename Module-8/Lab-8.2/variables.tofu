variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ide_instance_id" {
  type = string
}

variable "extra_availability_zone" {
  type = string
}

variable "public_route_table_id" {
  type = string
}

variable "engine_version" {
  type = string
}

variable "solution_stack_name" {
  type = string
}

variable "client_cidr" {
  type = string
  validation {
    condition     = can(cidrnetmask(var.client_cidr)) && endswith(var.client_cidr, "/32")
    error_message = "Use your public IPv4 address followed by /32."
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
