variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ide_instance_id" {
  type = string
}

variable "mysql_instance_id" {
  type = string
}

variable "app_instance_id" {
  type = string
}

variable "client_cidr" {
  type = string

  validation {
    condition     = can(cidrnetmask(var.client_cidr)) && endswith(var.client_cidr, "/32")
    error_message = "client_cidr must identify one IPv4 address with /32."
  }
}

variable "prepare_grading" {
  type    = bool
  default = false
}
