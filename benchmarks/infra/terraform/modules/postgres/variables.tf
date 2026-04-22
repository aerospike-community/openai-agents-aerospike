variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
}

variable "topology" {
  description = "`standalone` (1 node) or `replicated` (1 primary + 2 async streaming replicas)."
  type        = string
  validation {
    condition     = contains(["standalone", "replicated"], var.topology)
    error_message = "topology must be one of: standalone, replicated."
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

variable "subnet_cidr" {
  description = "VPC subnet CIDR. Plumbed to pg_hba.conf as the allowed replication + client range."
  type        = string
}

variable "local_ssd_count" {
  description = "Local NVMe SSDs per node. Formatted as ext4 and mounted at /var/lib/postgresql/<major>/main for data + WAL."
  type        = number
  default     = 1
}

variable "postgres_major_version" {
  description = "Postgres major version from the PGDG apt repo."
  type        = number
  default     = 16
}

variable "db_name" {
  description = "Benchmark database name. Created on the primary at first boot."
  type        = string
  default     = "bench"
}

variable "db_user" {
  description = "Benchmark role name. Password generated at the root level and passed in."
  type        = string
  default     = "bench"
}

variable "db_password" {
  description = "Benchmark role password. Generated via random_password in the root."
  type        = string
  sensitive   = true
}

variable "replication_user" {
  description = "Dedicated replication role. Used by replicas for pg_basebackup + streaming."
  type        = string
  default     = "replicator"
}

variable "replication_password" {
  description = "Replication role password. Generated via random_password in the root."
  type        = string
  sensitive   = true
}

variable "synchronous_commit" {
  description = "postgresql.conf synchronous_commit. `on` = durable per commit (headline). `off` = fsync group commit (durability variant)."
  type        = string
  default     = "on"
  validation {
    condition     = contains(["on", "off", "local", "remote_write", "remote_apply"], var.synchronous_commit)
    error_message = "synchronous_commit must be a valid postgresql.conf value."
  }
}

variable "labels" {
  description = "GCP labels applied to each instance."
  type        = map(string)
  default     = {}
}
