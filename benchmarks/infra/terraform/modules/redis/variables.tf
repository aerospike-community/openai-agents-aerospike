variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
}

variable "topology" {
  description = "`standalone` (1 node, no replication) or `sentinel` (1 primary + 2 replicas, each node also runs redis-sentinel)."
  type        = string
  validation {
    condition     = contains(["standalone", "sentinel"], var.topology)
    error_message = "topology must be one of: standalone, sentinel."
  }
}

variable "zone" {
  description = "GCP zone."
  type        = string
}

variable "machine_type" {
  description = "Instance type."
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

variable "local_ssd_count" {
  description = "Local NVMe SSDs per node for AOF persistence. Formatted as ext4 and mounted at /var/lib/redis so fsync latency matches Aerospike's physical storage class."
  type        = number
  default     = 1
}

variable "redis_version" {
  description = "Redis major version (installed from the official packages.redis.io apt repo)."
  type        = string
  default     = "7"
}

variable "sentinel_quorum" {
  description = "Sentinel failover quorum. 2 of 3 is the standard choice."
  type        = number
  default     = 2
}

variable "master_name" {
  description = "Sentinel master name. Clients using Sentinel must reference this name."
  type        = string
  default     = "bench-master"
}

variable "labels" {
  description = "GCP labels applied to each instance."
  type        = map(string)
  default     = {}
}
