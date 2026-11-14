variable "region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  description = "t3.large (2 vCPU / 8 GiB) comfortably runs k3s, the monitoring stack and Jenkins"
  type        = string
  default     = "t3.large"
}

variable "repo_url" {
  description = "Git repository the instance clones and deploys"
  type        = string
  default     = "https://github.com/hardikOG/DEVOPS_project.git"
}

variable "repo_branch" {
  type    = string
  default = "main"
}

variable "allowed_cidr" {
  description = "CIDR allowed to reach the dashboards and Jenkins. Set to your IP/32."
  type        = string
  default     = "0.0.0.0/0"
}

variable "key_name" {
  description = "Optional EC2 key pair for SSH (e.g. vockey in AWS Academy). Leave null to disable SSH."
  type        = string
  default     = null
}

variable "root_volume_gb" {
  type    = number
  default = 30
}
