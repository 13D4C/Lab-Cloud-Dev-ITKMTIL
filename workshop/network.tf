locals {
  subnets = {
    public-a  = { zone = 0, cidr = "10.0.1.0/24", tier = "public" }
    public-b  = { zone = 1, cidr = "10.0.2.0/24", tier = "public" }
    private-a = { zone = 0, cidr = "10.0.11.0/24", tier = "private" }
    private-b = { zone = 1, cidr = "10.0.12.0/24", tier = "private" }
    db-a      = { zone = 0, cidr = "10.0.21.0/24", tier = "db" }
    db-b      = { zone = 1, cidr = "10.0.22.0/24", tier = "db" }
  }
}

resource "aws_vpc" "workshop" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "workshop-vpc" }
}

resource "aws_subnet" "workshop" {
  for_each                = local.subnets
  vpc_id                  = aws_vpc.workshop.id
  cidr_block              = each.value.cidr
  availability_zone       = var.availability_zones[each.value.zone]
  map_public_ip_on_launch = each.value.tier == "public"
  tags                    = { Name = "workshop-${each.key}" }
}

resource "aws_internet_gateway" "workshop" {
  vpc_id = aws_vpc.workshop.id
  tags   = { Name = "workshop-igw" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "workshop-nat-eip" }
}

resource "aws_nat_gateway" "workshop" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.workshop["public-a"].id
  tags          = { Name = "workshop-nat" }
  depends_on    = [aws_internet_gateway.workshop]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.workshop.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.workshop.id
  }
  tags = { Name = "workshop-rt-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.workshop.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.workshop.id
  }
  tags = { Name = "workshop-rt-private" }
}

resource "aws_route_table" "database" {
  vpc_id = aws_vpc.workshop.id
  tags   = { Name = "workshop-rt-db" }
}

resource "aws_route_table_association" "workshop" {
  for_each  = local.subnets
  subnet_id = aws_subnet.workshop[each.key].id
  route_table_id = each.value.tier == "public" ? aws_route_table.public.id : (
    each.value.tier == "private" ? aws_route_table.private.id : aws_route_table.database.id
  )
}
