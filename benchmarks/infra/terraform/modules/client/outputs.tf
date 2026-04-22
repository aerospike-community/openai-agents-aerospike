output "name" {
  description = "Instance name. Feed to `gcloud compute ssh` for orchestration."
  value       = google_compute_instance.this.name
}

output "internal_ip" {
  description = "Internal IP."
  value       = google_compute_instance.this.network_interface[0].network_ip
}

output "external_ip" {
  description = "External IP. Used by `gcloud compute ssh` via IAP / direct."
  value       = google_compute_instance.this.network_interface[0].access_config[0].nat_ip
}
