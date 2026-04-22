variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
}

variable "zone" {
  description = "GCP zone."
  type        = string
}

variable "machine_type" {
  description = "Instance type. Match the DB class so client CPU is headroom, not a bottleneck."
  type        = string
  default     = "n2d-standard-8"
}

variable "boot_image" {
  description = "Base image."
  type        = string
  default     = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
}

variable "subnet_self_link" {
  description = "VPC subnet self_link."
  type        = string
}

variable "repo_url" {
  description = "Git repository to clone on the client. Override only if you're driving the sweep from a fork."
  type        = string
  default     = "https://github.com/aerospike-community/openai-agents-aerospike.git"
}

variable "repo_ref" {
  description = "Git ref (branch, tag, or SHA) to check out on the client. Pin to a tag for reproducible runs."
  type        = string
  default     = "main"
}

variable "labels" {
  description = "GCP labels applied to the instance."
  type        = map(string)
  default     = {}
}
