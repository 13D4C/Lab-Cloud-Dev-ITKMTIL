resource "aws_security_group" "alb" {
  name   = "workshop-sg-alb"
  vpc_id = aws_vpc.workshop.id
  ingress {
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "workshop-sg-alb" }
}

resource "aws_security_group" "ec2" {
  name   = "workshop-sg-ec2"
  vpc_id = aws_vpc.workshop.id
  dynamic "ingress" {
    for_each = toset([3000, 8080])
    content {
      protocol        = "tcp"
      from_port       = ingress.value
      to_port         = ingress.value
      security_groups = [aws_security_group.alb.id]
    }
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "workshop-sg-ec2" }
}

resource "aws_security_group" "rds" {
  name   = "workshop-sg-rds"
  vpc_id = aws_vpc.workshop.id
  ingress {
    protocol        = "tcp"
    from_port       = 5432
    to_port         = 5432
    security_groups = [aws_security_group.ec2.id]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "workshop-sg-rds" }
}
