output "nodes" {
  description = "Per-node info, in stable order (node 0 is the primary in sentinel topology)."
  value = [
    for inst in google_compute_instance.node : {
      name        = inst.name
      internal_ip = inst.network_interface[0].network_ip
      external_ip = inst.network_interface[0].access_config[0].nat_ip
    }
  ]
}

output "primary_internal_ip" {
  description = "Internal IP of the primary node (node 0). Equal to the sole node IP in standalone topology."
  value       = google_compute_instance.node[0].network_interface[0].network_ip
}

output "sentinel_hosts" {
  description = "Internal IPs of all sentinel-running nodes (empty list in standalone topology). Use for sentinel-aware client config."
  value = var.topology == "sentinel" ? [
    for inst in google_compute_instance.node : inst.network_interface[0].network_ip
  ] : []
}

output "master_name" {
  description = "Sentinel master name (same in both topologies, ignored by clients in standalone mode)."
  value       = var.master_name
}

output "topology" {
  description = "Echo the topology so the root module can embed it in outputs."
  value       = var.topology
}
