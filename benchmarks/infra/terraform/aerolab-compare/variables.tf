variable "project_id" {
  description = "GCP project ID. Must match the project where the aerolab cluster lives."
  type        = string
}

variable "region" {
  description = "GCP region. Must match the aerolab cluster's region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone. Put the client in the same zone as the aerolab cluster to avoid cross-AZ latency."
  type        = string
  default     = "us-central1-a"
}

variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
  default     = "bench-aerolab"
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

variable "aerospike_seed_ip" {
  description = "Internal IP of any aerolab cluster node. Exposed via output for the orchestrator."
  type        = string
}

variable "aerospike_namespace" {
  description = "Namespace the aerolab cluster serves. aerolab defaults to `test`."
  type        = string
  default     = "test"
}
