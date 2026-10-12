provider "aws" {
  region = var.aws_region
}

data "aws_instance" "ide" {
  instance_id = var.ide_instance_id
}

data "aws_instance" "mysql" {
  instance_id = var.mysql_instance_id
}

data "aws_instance" "app" {
  instance_id = var.app_instance_id
}

resource "aws_ecr_repository" "node_app" {
  name                 = "node-app"
  image_tag_mutability = "MUTABLE"

  lifecycle {
    ignore_changes = [tags]
  }
}

resource "aws_vpc_security_group_ingress_rule" "application" {
  security_group_id = one(data.aws_instance.ide.vpc_security_group_ids)
  cidr_ipv4         = var.client_cidr
  ip_protocol       = "tcp"
  from_port         = 3000
  to_port           = 3000
  description       = "Lab 8.1 coffee suppliers"
}

resource "aws_vpc_security_group_ingress_rule" "grading" {
  count             = var.prepare_grading ? 1 : 0
  security_group_id = one(data.aws_instance.ide.vpc_security_group_ids)
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  description       = "AWS Academy Lab 8.1 grading"
}
