resource "aws_db_subnet_group" "workshop" {
  name       = "workshop-db-subnet-group"
  subnet_ids = [aws_subnet.workshop["db-a"].id, aws_subnet.workshop["db-b"].id]
  tags       = { Name = "workshop-db-subnet-group" }
}

resource "aws_db_instance" "workshop" {
  identifier                 = "workshop-db"
  engine                     = "postgres"
  engine_version             = var.db_engine_version
  instance_class             = var.db_instance_class
  allocated_storage          = 20
  storage_type               = "gp2"
  storage_encrypted          = true
  username                   = "postgres"
  password                   = var.db_password
  db_name                    = "itkmitl"
  multi_az                   = true
  publicly_accessible        = false
  db_subnet_group_name       = aws_db_subnet_group.workshop.name
  vpc_security_group_ids     = [aws_security_group.rds.id]
  backup_retention_period    = 7
  auto_minor_version_upgrade = true
  deletion_protection        = false
  skip_final_snapshot        = true
  delete_automated_backups   = true
  apply_immediately          = true
  tags                       = { Name = "workshop-db" }
  lifecycle {
    ignore_changes = [engine_version]
  }
}
