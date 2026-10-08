# Red privada (VPC) donde viven todos los nodos de DFSha.
# Los nodos se hablan por IP privada; desde Internet solo quedan abiertos los
# puertos de las APIs que usa el cliente (8000 ControlNode y 8100 DataNode).

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

resource "aws_vpc" "dfsha" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "dfsha-vpc", Project = "DFSha" }
}

resource "aws_internet_gateway" "dfsha" {
  vpc_id = aws_vpc.dfsha.id
  tags   = { Name = "dfsha-igw", Project = "DFSha" }
}

resource "aws_subnet" "nodes" {
  vpc_id                  = aws_vpc.dfsha.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = true
  tags                    = { Name = "dfsha-nodes", Project = "DFSha" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.dfsha.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.dfsha.id
  }

  tags = { Name = "dfsha-public", Project = "DFSha" }
}

resource "aws_route_table_association" "nodes" {
  subnet_id      = aws_subnet.nodes.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "nodes" {
  name        = "dfsha-nodes"
  description = "DFSha: APIs publicas para el cliente y trafico libre solo entre nodos"
  vpc_id      = aws_vpc.dfsha.id

  ingress {
    description = "API del ControlNode (cliente)"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.client_cidr]
  }

  ingress {
    description = "API del DataNode (cliente)"
    from_port   = 8100
    to_port     = 8100
    protocol    = "tcp"
    cidr_blocks = [var.client_cidr]
  }

  ingress {
    description = "SSH para administracion"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  ingress {
    description = "Trafico interno entre nodos de DFSha"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dfsha-nodes", Project = "DFSha" }
}
