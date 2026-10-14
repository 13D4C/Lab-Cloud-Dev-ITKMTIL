variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type = string
}

variable "availability_zones" {
  type = list(string)
  validation {
    condition     = length(var.availability_zones) == 2
    error_message = "Select two availability zones."
  }
}

variable "ami_id" {
  type = string
}

variable "instance_profile" {
  type    = string
  default = "LabInstanceProfile"
}

variable "lab_role_arn" {
  type    = string
  default = null
}

variable "instance_type" {
  type    = string
  default = "t3.micro"
}

variable "ssh_public_key" {
  type = string
}

variable "db_engine_version" {
  type = string
}

variable "db_instance_class" {
  type    = string
  default = "db.t3.micro"
}

variable "db_password" {
  type      = string
  sensitive = true
  validation {
    condition     = length(var.db_password) >= 16 && can(regex("^[A-Za-z0-9!_-]+$", var.db_password))
    error_message = "Use at least 16 characters: letters, digits, !, _ or -."
  }
}

variable "jwt_secret" {
  type      = string
  sensitive = true
  validation {
    condition     = length(var.jwt_secret) >= 32 && can(regex("^[A-Za-z0-9_-]+$", var.jwt_secret))
    error_message = "Use at least 32 letters, digits, underscores or hyphens."
  }
}

variable "assets_bucket_name" {
  type = string
}

variable "source_repository" {
  type    = string
  default = "https://github.com/13D4C/Workshop-IT-KMITL-Cloud-Computing-Deployment-Website-Production.git"
}

variable "source_revision" {
  type    = string
  default = "cbd8bcd77e5125619015309f9fc1f642b85a60ff"
}
