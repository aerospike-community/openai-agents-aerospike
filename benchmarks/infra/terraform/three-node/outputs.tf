output "topology" {
  description = "Shape of this deployment. Orchestrator uses this to stamp results."
  value       = "three-node"
}

output "zone" {
  description = "GCP zone all VMs live in. Orchestrator passes this to `gcloud compute ssh --zone=...`."
  value       = var.zone
}

output "aerospike_nodes" {
  description = "All Aerospike nodes."
  value       = module.aerospike.nodes
}

output "aerospike_seed_ip" {
  description = "Seed IP for AEROSPIKE_HOST. Client auto-discovers the rest via the cluster protocol."
  value       = module.aerospike.seed_internal_ip
}

output "redis_nodes" {
  description = "All Redis nodes. Node 0 is the initial primary (Sentinel may fail over)."
  value       = module.redis.nodes
}

output "redis_primary_ip" {
  description = "Initial Redis primary IP. For long-running sweeps where failover may happen, prefer sentinel-aware connection."
  value       = module.redis.primary_internal_ip
}

output "redis_sentinel_hosts" {
  description = "Sentinel endpoints (port 26379). Use these for sentinel-aware clients."
  value       = module.redis.sentinel_hosts
}

output "redis_url" {
  description = "Convenience REDIS_URL pointing at the initial primary. OK for sweeps without planned failover; otherwise use redis_sentinel_hosts."
  value       = "redis://${module.redis.primary_internal_ip}:6379/0"
}

output "postgres_nodes" {
  description = "All Postgres nodes. Node 0 is the primary; nodes 1-2 are async streaming replicas."
  value       = module.postgres.nodes
}

output "postgres_primary_ip" {
  description = "Primary internal IP — client connects here for writes."
  value       = module.postgres.primary_internal_ip
}

output "postgres_replica_ips" {
  description = "Async replica internal IPs."
  value       = module.postgres.replica_internal_ips
}

output "sqlalchemy_url" {
  description = "SQLALCHEMY_URL pointing at the primary."
  value       = "postgresql+asyncpg://bench:${random_password.postgres.result}@${module.postgres.primary_internal_ip}:5432/bench"
  sensitive   = true
}

output "postgres_password" {
  description = "Plain text of the Postgres bench role password."
  value       = random_password.postgres.result
  sensitive   = true
}

output "client_name" {
  description = "Client VM name. Pass to `gcloud compute ssh <name>`."
  value       = module.client.name
}
