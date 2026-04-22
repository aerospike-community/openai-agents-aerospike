variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone. All nodes share one zone to remove cross-AZ variance from measurements."
  type        = string
  default     = "us-central1-a"
}

variable "allowed_ssh_cidr" {
  description = "Your workstation's external IP as /32. Required: VMs have public IPs."
  type        = string
}

variable "aerospike_features_conf_path" {
  description = "Path to a valid Aerospike EE feature-key file for a multi-node cluster."
  type        = string
}

variable "aerospike_server_version" {
  description = "Aerospike Enterprise server version."
  type        = string
  default     = "7.2.0.3"
}

variable "aerospike_replication_factor" {
  description = "Replication factor for the bench namespace. 2 = production-minimum, tolerates one node loss."
  type        = number
  default     = 2
}

variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
  default     = "bench-3n"
}

variable "repo_url" {
  description = "Git repo cloned on the client VM."
  type        = string
  default     = "https://github.com/aerospike-community/openai-agents-aerospike.git"
}

variable "repo_ref" {
  description = "Git ref checked out on the client VM."
  type        = string
  default     = "main"
}
