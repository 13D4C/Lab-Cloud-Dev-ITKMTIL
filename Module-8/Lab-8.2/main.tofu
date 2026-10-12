data "aws_instance" "ide" {
  instance_id = var.ide_instance_id
}

data "aws_subnet" "ide" {
  id = data.aws_instance.ide.subnet_id
}

data "aws_ecr_repository" "application" {
  name = "cafe/node-web-app"
}

locals {
  ide_security_group = one(data.aws_instance.ide.vpc_security_group_ids)
  settings = {
    "aws:autoscaling:launchconfiguration/IamInstanceProfile"   = "aws-elasticbeanstalk-ec2-role"
    "aws:autoscaling:launchconfiguration/SecurityGroups"       = local.ide_security_group
    "aws:autoscaling:launchconfiguration/InstanceType"         = "t3.micro"
    "aws:autoscaling:launchconfiguration/DisableIMDSv1"        = "true"
    "aws:autoscaling:launchconfiguration/RootVolumeType"       = "gp2"
    "aws:autoscaling:launchconfiguration/RootVolumeSize"       = "10"
    "aws:autoscaling:asg/MinSize"                              = "1"
    "aws:autoscaling:asg/MaxSize"                              = "1"
    "aws:ec2:vpc/VPCId"                                        = data.aws_subnet.ide.vpc_id
    "aws:ec2:vpc/Subnets"                                      = join(",", [data.aws_instance.ide.subnet_id, aws_subnet.extra.id])
    "aws:ec2:vpc/ELBSubnets"                                   = join(",", [data.aws_instance.ide.subnet_id, aws_subnet.extra.id])
    "aws:ec2:vpc/AssociatePublicIpAddress"                     = "true"
    "aws:elasticbeanstalk:environment/EnvironmentType"         = "LoadBalanced"
    "aws:elasticbeanstalk:environment/LoadBalancerType"        = "classic"
    "aws:elasticbeanstalk:healthreporting:system/SystemType"   = "basic"
    "aws:elasticbeanstalk:application:environment/APP_DB_HOST" = aws_rds_cluster.database.endpoint
  }
}

resource "aws_subnet" "extra" {
  vpc_id                  = data.aws_subnet.ide.vpc_id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = var.extra_availability_zone
  map_public_ip_on_launch = true
  tags                    = { Name = "extraSubnetForRds" }
}

resource "aws_route_table_association" "extra" {
  subnet_id      = aws_subnet.extra.id
  route_table_id = var.public_route_table_id
}

resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = local.ide_security_group
  referenced_security_group_id = local.ide_security_group
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
}

resource "aws_vpc_security_group_ingress_rule" "preview" {
  security_group_id = local.ide_security_group
  cidr_ipv4         = var.client_cidr
  ip_protocol       = "tcp"
  from_port         = 8000
  to_port           = 8000
}

resource "aws_db_subnet_group" "database" {
  name       = "supplierdb-subnets"
  subnet_ids = [data.aws_instance.ide.subnet_id, aws_subnet.extra.id]
}

resource "aws_rds_cluster" "database" {
  cluster_identifier     = "supplierdb"
  engine                 = "aurora-mysql"
  engine_mode            = "provisioned"
  engine_version         = var.engine_version
  database_name          = "suppliers"
  master_username        = "admin"
  master_password        = "coffee_beans_for_all"
  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [local.ide_security_group]
  enable_http_endpoint   = true
  skip_final_snapshot    = true
  deletion_protection    = false
  storage_encrypted      = true
  apply_immediately      = true
  serverlessv2_scaling_configuration {
    min_capacity = 2
    max_capacity = 16
  }
  depends_on = [aws_route_table_association.extra, aws_vpc_security_group_ingress_rule.database]
}

resource "aws_rds_cluster_instance" "database" {
  identifier                   = "supplierdb-instance-1"
  cluster_identifier           = aws_rds_cluster.database.id
  engine                       = aws_rds_cluster.database.engine
  engine_version               = aws_rds_cluster.database.engine_version
  instance_class               = "db.serverless"
  db_subnet_group_name         = aws_db_subnet_group.database.name
  publicly_accessible          = false
  performance_insights_enabled = false
  monitoring_interval          = 0
  apply_immediately            = true
}

resource "aws_elastic_beanstalk_application" "application" {
  name = "MyNodeApp"
}

resource "aws_s3_object" "application" {
  bucket = var.site_bucket
  key    = "lab82/application-${filemd5("${path.module}/.tools/application.zip")}.zip"
  source = "${path.module}/.tools/application.zip"
  etag   = filemd5("${path.module}/.tools/application.zip")
}

resource "aws_elastic_beanstalk_application_version" "application" {
  name        = "MyNodeApp-${filemd5("${path.module}/.tools/application.zip")}"
  application = aws_elastic_beanstalk_application.application.name
  bucket      = aws_s3_object.application.bucket
  key         = aws_s3_object.application.key
}

resource "aws_elastic_beanstalk_environment" "application" {
  name                   = "MyEnv"
  application            = aws_elastic_beanstalk_application.application.name
  solution_stack_name    = var.solution_stack_name
  version_label          = aws_elastic_beanstalk_application_version.application.name
  wait_for_ready_timeout = "40m"
  dynamic "setting" {
    for_each = local.settings
    content {
      namespace = split("/", setting.key)[0]
      name      = split("/", setting.key)[1]
      value     = setting.value
    }
  }
  depends_on = [aws_route_table_association.extra, aws_rds_cluster_instance.database]
}
