output "topology" {
  description = "Shape of this deployment. Orchestrator uses this to stamp results."
  value       = "single-node"
}

output "zone" {
  description = "GCP zone all VMs live in. Orchestrator passes this to `gcloud compute ssh --zone=...`."
  value       = var.zone
}

output "aerospike_nodes" {
  description = "Aerospike node details (name, internal_ip, external_ip)."
  value       = module.aerospike.nodes
}

output "aerospike_seed_ip" {
  description = "AEROSPIKE_HOST value for the client VM."
  value       = module.aerospike.seed_internal_ip
}

output "redis_primary_ip" {
  description = "Redis primary IP. REDIS_URL=redis://<this>:6379/0 in standalone topology."
  value       = module.redis.primary_internal_ip
}

output "redis_url" {
  description = "Ready-to-use REDIS_URL for the harness."
  value       = "redis://${module.redis.primary_internal_ip}:6379/0"
}

output "postgres_primary_ip" {
  description = "Postgres primary internal IP."
  value       = module.postgres.primary_internal_ip
}

output "sqlalchemy_url" {
  description = "SQLALCHEMY_URL for the harness."
  value       = "postgresql+asyncpg://bench:${random_password.postgres.result}@${module.postgres.primary_internal_ip}:5432/bench"
  sensitive   = true
}

output "postgres_password" {
  description = "Plain text of the Postgres bench role password. Use `terraform output -raw postgres_password` to retrieve."
  value       = random_password.postgres.result
  sensitive   = true
}

output "client_name" {
  description = "Client VM name. Pass to `gcloud compute ssh <name>`."
  value       = module.client.name
}
