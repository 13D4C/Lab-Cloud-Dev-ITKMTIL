resource "aws_vpc_security_group_ingress_rule" "cache_self" {
  security_group_id            = var.cache_security_group_id
  referenced_security_group_id = var.cache_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 11211
  to_port                      = 11211
}

resource "aws_vpc_security_group_ingress_rule" "cache_ide" {
  security_group_id            = var.cache_security_group_id
  referenced_security_group_id = var.ide_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 11211
  to_port                      = 11211
}

resource "aws_elasticache_subnet_group" "cache" {
  name        = "elasticachesubnetgroup"
  description = "Subnet Group for ElastiCache"
  subnet_ids  = var.subnet_ids
}

resource "aws_elasticache_cluster" "cache" {
  cluster_id           = "memcachedcache"
  engine               = "memcached"
  engine_version       = "1.6.6"
  node_type            = "cache.r6g.large"
  num_cache_nodes      = 3
  parameter_group_name = "default.memcached1.6"
  port                 = 11211
  subnet_group_name    = aws_elasticache_subnet_group.cache.name
  security_group_ids   = [var.cache_security_group_id]
  az_mode              = "cross-az"
  apply_immediately    = true

  depends_on = [
    aws_vpc_security_group_ingress_rule.cache_self,
    aws_vpc_security_group_ingress_rule.cache_ide
  ]
}

resource "aws_s3_object" "application" {
  bucket       = var.site_bucket
  key          = "lab91/application-${filemd5("${path.module}/.tools/application.zip")}.zip"
  source       = "${path.module}/.tools/application.zip"
  etag         = filemd5("${path.module}/.tools/application.zip")
  content_type = "application/zip"
}

resource "aws_elastic_beanstalk_application_version" "application" {
  count       = var.create_application_version ? 1 : 0
  name        = "Lab91-${filemd5("${path.module}/.tools/application.zip")}"
  application = "MyNodeApp"
  bucket      = aws_s3_object.application.bucket
  key         = aws_s3_object.application.key
}
