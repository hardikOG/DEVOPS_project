data "aws_vpc" "default" {
  default = true
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

locals {
  # name => port; every UI the demo needs, all restricted to allowed_cidr.
  ports = {
    aegis        = 30080
    grafana      = 30030
    prometheus   = 30090
    alertmanager = 30093
    jenkins      = 8080
  }
}

resource "aws_security_group" "aegis" {
  name_prefix = "aegis-"
  description = "Aegis self-healing demo"
  vpc_id      = data.aws_vpc.default.id

  dynamic "ingress" {
    for_each = local.ports
    content {
      description = ingress.key
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = [var.allowed_cidr]
    }
  }

  dynamic "ingress" {
    for_each = var.key_name == null ? [] : [22]
    content {
      description = "ssh"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [var.allowed_cidr]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle { create_before_destroy = true }
}

resource "aws_instance" "aegis" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.aegis.id]

  # Bootstrapping runs on the instance itself, so no inbound SSH is required to deploy.
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url    = var.repo_url
    repo_branch = var.repo_branch
  })
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "aegis-self-healing" }
}
