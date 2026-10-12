resource "aws_secretsmanager_secret" "query_editor" {
  name        = "rds-db-credentials/${aws_rds_cluster.database.cluster_resource_id}/admin"
  description = "Aurora supplierdb credentials for the RDS query editor"
}

resource "aws_secretsmanager_secret_version" "query_editor" {
  secret_id = aws_secretsmanager_secret.query_editor.id
  secret_string = jsonencode({
    engine              = "mysql"
    host                = aws_rds_cluster.database.endpoint
    port                = 3306
    username            = aws_rds_cluster.database.master_username
    password            = aws_rds_cluster.database.master_password
    dbname              = "suppliers"
    dbClusterIdentifier = aws_rds_cluster.database.cluster_identifier
  })
}
