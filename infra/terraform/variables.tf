variable "aws_region" {
  description = "Región de AWS (AWS Academy Learner Lab usa us-east-1)"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "Tipo de instancia EC2 para todos los nodos"
  type        = string
  default     = "t3.small"
}

variable "key_name" {
  description = "Key pair para entrar por SSH (en el Learner Lab ya existe vockey)"
  type        = string
  default     = "vockey"
}

variable "datanode_count" {
  description = "Cantidad de DataNodes. Para escalar horizontalmente se cambia este número"
  type        = number
  default     = 4

  validation {
    condition     = var.datanode_count >= 3 && var.datanode_count <= 6
    error_message = "Use entre 3 y 6 DataNodes (hay certificados generados para 6)."
  }
}

variable "repo_url" {
  description = "Repositorio público desde donde cada EC2 descarga el código"
  type        = string
  default     = "https://github.com/matias-martinez-moreno/DFSha-Sistema-de-Archivos-Distribuido-por-Bloques-con-Alta-Disponibilidad-sobre-AWS.git"
}

variable "repo_branch" {
  description = "Rama del repositorio a desplegar"
  type        = string
  default     = "main"
}

variable "admin_password" {
  description = "Contraseña del usuario admin de DFSha"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.admin_password) >= 8
    error_message = "La contraseña del admin debe tener al menos 8 caracteres."
  }
}

variable "allowed_ssh_cidr" {
  description = "Desde dónde se permite SSH (idealmente su IP pública con /32)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "client_cidr" {
  description = "Desde dónde pueden conectarse los clientes DFSha a las APIs"
  type        = string
  default     = "0.0.0.0/0"
}

variable "replication_factor" {
  description = "Cuántas copias se guardan de cada bloque"
  type        = number
  default     = 3
}

variable "min_replicas" {
  description = "Cuántas copias tienen que confirmarse para aceptar una escritura"
  type        = number
  default     = 2
}

variable "block_size_mb" {
  description = "Tamaño de bloque en MB"
  type        = number
  default     = 4
}

variable "ebs_encrypted" {
  description = "Cifrar los discos EBS de las instancias"
  type        = bool
  default     = true
}
