variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
}

variable "node_count" {
  description = "Number of Aerospike nodes. 1 for single-node dev / baseline, 3 for the HA / sharded production shape."
  type        = number
}

variable "zone" {
  description = "GCP zone. All nodes co-located per benchmark plan."
  type        = string
}

variable "machine_type" {
  description = "Instance type. Default n2d-standard-8 across the fleet."
  type        = string
  default     = "n2d-standard-8"
}

variable "boot_image" {
  description = "Base image."
  type        = string
  default     = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
}

variable "subnet_self_link" {
  description = "VPC subnet self_link from the network module."
  type        = string
}

variable "local_ssd_count" {
  description = "Local NVMe SSDs per node. 375 GB each, raw block device. Benchmark default is 1."
  type        = number
  default     = 1
}

variable "device_partitions_per_ssd" {
  description = "Partitions per NVMe device. Aerospike scales storage-engine parallelism with device count; 4 partitions per 375 GB NVMe is the recommended starting point for NVMe on modern servers."
  type        = number
  default     = 4
}

variable "replication_factor" {
  description = "Replication factor for the bench namespace. Clamped to <= node_count at provisioning time."
  type        = number
  default     = 2
}

variable "namespace" {
  description = "Aerospike namespace name."
  type        = string
  default     = "bench"
}

variable "server_version" {
  description = "Aerospike Enterprise server version to install."
  type        = string
  default     = "7.2.0.3"
}

variable "features_conf_path" {
  description = "Path (from module caller) to a valid Aerospike EE feature-key file. Multi-node keys required for node_count > 1."
  type        = string
}

variable "commit_to_device" {
  description = "Aerospike commit-to-device flag. false = async flush (headline, vendor default). true = synchronous commit to disk for each write (durability variant)."
  type        = bool
  default     = false
}

variable "labels" {
  description = "GCP labels applied to each instance."
  type        = map(string)
  default     = {}
}
