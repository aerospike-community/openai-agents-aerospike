variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH in. Use your workstation's public IP (/32) — VMs have public IPs."
  type        = string
}

variable "subnet_cidr" {
  description = "Private CIDR for the benchmark subnet. Anything RFC1918 works; 10.100.0.0/24 gives us 254 usable addresses, plenty."
  type        = string
  default     = "10.100.0.0/24"
}
