variable "name" {
  description = "Prefix for every resource."
  type        = string
  default     = "collabdoc"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  # c7i-flex.large rather than t3.medium: accounts on the AWS Free plan may
  # only launch free-tier-eligible types, and this is the 4 GB one.
  description = "The k3s node. 4 GB holds Kafka, Caddy and three app pods."
  type        = string
  default     = "c7i-flex.large"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "admin_cidr" {
  description = "CIDR allowed to reach the Kubernetes API. Empty means your current public IP."
  type        = string
  default     = ""
}
