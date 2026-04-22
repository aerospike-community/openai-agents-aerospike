output "topology" {
  description = "Shape of this deployment. Orchestrator uses this to stamp results."
  value       = "aerolab-compare"
}

output "zone" {
  description = "GCP zone the client lives in."
  value       = var.zone
}

output "client_name" {
  description = "Client VM name. Pass to `gcloud compute ssh <name>`."
  value       = module.client.name
}

output "aerospike_seed_ip" {
  description = "Seed IP for AEROSPIKE_HOST — echoed from input, provided for orchestrator parity."
  value       = var.aerospike_seed_ip
}

output "aerospike_namespace" {
  description = "Namespace served by the external cluster. Orchestrator passes it as AEROSPIKE_NAMESPACE."
  value       = var.aerospike_namespace
}
