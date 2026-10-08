# Una EC2 por nodo. Cada una instala Docker, descarga el repositorio, construye
# la imagen y arranca su contenedor (ControlNode o DataNode) con su configuración.
# Las IP privadas son fijas para que cada nodo sepa de antemano dónde están los demás.

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "random_password" "internal" {
  length  = 48
  special = false
}

resource "random_password" "block_token" {
  length  = 48
  special = false
}

resource "random_password" "storage" {
  for_each = local.datanode_ips
  length   = 48
  special  = false
}

locals {
  certs_dir = "${path.module}/../../certs"

  controlnode_ips = {
    controlnode1 = "10.0.1.10"
    controlnode2 = "10.0.1.11"
  }
  datanode_ips = { for i in range(var.datanode_count) : "datanode${i + 1}" => "10.0.1.${21 + i}" }

  control_internal_urls = [for name, ip in local.controlnode_ips : "https://${ip}:8000"]

  common_env = {
    JWT_SECRET         = random_password.jwt.result
    INTERNAL_KEY       = random_password.internal.result
    BLOCK_TOKEN_SECRET = random_password.block_token.result
    ADMIN_PASSWORD     = var.admin_password
    TLS_CA             = "/certs/ca.crt"
    TLS_CERT           = "/certs/node.crt"
    TLS_KEY            = "/certs/node.key"
    INTERNAL_CIDRS     = aws_vpc.dfsha.cidr_block
    REPLICATION_FACTOR = tostring(var.replication_factor)
    MIN_REPLICAS       = tostring(var.min_replicas)
    BLOCK_SIZE         = tostring(var.block_size_mb * 1024 * 1024)
  }

  controlnodes = {
    for name, ip in local.controlnode_ips : name => {
      component  = "controlnode"
      private_ip = ip
      port       = 8000
      env = merge(local.common_env, {
        NODE_ID      = name
        PORT         = "8000"
        INTERNAL_URL = "https://${ip}:8000"
        PEER_URL     = "https://${name == "controlnode1" ? local.controlnode_ips.controlnode2 : local.controlnode_ips.controlnode1}:8000"
        DB_PATH      = "/data/metadata.db"
      })
    }
  }

  datanodes = {
    for name, ip in local.datanode_ips : name => {
      component  = "datanode"
      private_ip = ip
      port       = 8100
      env = merge(local.common_env, {
        NODE_ID      = name
        PORT         = "8100"
        INTERNAL_URL = "https://${ip}:8100"
        CONTROL_URLS = join(",", local.control_internal_urls)
        DATA_DIR     = "/data"
        STORAGE_KEY  = random_password.storage[name].result
      })
    }
  }

  nodes = merge(local.controlnodes, local.datanodes)
}

resource "aws_instance" "node" {
  for_each = local.nodes

  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  subnet_id              = aws_subnet.nodes.id
  private_ip             = each.value.private_ip
  vpc_security_group_ids = [aws_security_group.nodes.id]

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    node_name   = each.key
    component   = each.value.component
    port        = each.value.port
    env         = each.value.env
    repo_url    = var.repo_url
    repo_branch = var.repo_branch
    ca_crt      = file("${local.certs_dir}/ca.crt")
    node_crt    = file("${local.certs_dir}/${each.key}.crt")
    node_key    = file("${local.certs_dir}/${each.key}.key")
  })
  user_data_replace_on_change = true

  root_block_device {
    volume_size = 16
    volume_type = "gp3"
    encrypted   = var.ebs_encrypted
  }

  metadata_options {
    http_tokens = "required"
  }

  tags = {
    Name    = "dfsha-${each.key}"
    Project = "DFSha"
    Role    = each.value.component
  }

  depends_on = [aws_route_table_association.nodes]
}
