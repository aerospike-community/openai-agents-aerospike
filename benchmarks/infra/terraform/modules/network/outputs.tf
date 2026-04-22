output "subnet_self_link" {
  description = "Subnetwork self_link — pass to every VM's network_interface."
  value       = google_compute_subnetwork.this.self_link
}

output "subnet_cidr" {
  description = "Subnet CIDR — useful for Postgres pg_hba trust rules."
  value       = google_compute_subnetwork.this.ip_cidr_range
}

output "network_name" {
  description = "Network name."
  value       = google_compute_network.this.name
}
