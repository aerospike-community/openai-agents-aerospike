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
  description = "GCP zone. All VMs share one zone to remove cross-AZ variance from measurements."
  type        = string
  default     = "us-central1-a"
}

variable "allowed_ssh_cidr" {
  description = "Your workstation's external IP as /32. Required: VMs have public IPs and SSH is restricted to this range."
  type        = string
}

variable "aerospike_features_conf_path" {
  description = "Path to a valid Aerospike EE feature-key file (multi-node key works fine for single-node)."
  type        = string
}

variable "aerospike_server_version" {
  description = "Aerospike Enterprise server version. 8.0 is the current GA line."
  type        = string
  default     = "8.0.0.15"
}

variable "name_prefix" {
  description = "Resource name prefix for cost attribution and readability."
  type        = string
  default     = "bench-sn"
}

variable "repo_url" {
  description = "Git repo cloned onto the client VM. Override if running from a fork."
  type        = string
  default     = "https://github.com/aerospike-community/openai-agents-aerospike.git"
}

variable "repo_ref" {
  description = "Git ref (branch / tag / SHA) checked out on the client VM."
  type        = string
  default     = "main"
}
