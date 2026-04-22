// Throwaway topology: single client VM in the GCP default VPC so it can
// hit an already-provisioned aerolab-built Aerospike cluster on 10.128.0.*.
//
// Why this exists: aerolab is the reference way to stand up Aerospike on
// GCP. Running the same session_latency.py sweep against an aerolab cluster
// validates that our own terraform+startup.sh aerospike-cluster module is
// not leaving performance on the table. If numbers match ours, great. If
// aerolab wins by a meaningful margin, our module is the thing to fix.
//
// This topology does NOT provision the Aerospike nodes. It only provisions
// the client. The aerolab cluster is expected to be live already.

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  labels = {
    project  = "openai-agents-aerospike"
    purpose  = "benchmark"
    topology = "aerolab-compare"
  }
}

// Look up the default subnet in the target region. aerolab deploys into
// the default VPC, so the client has to live there too to reach the DB
// nodes on their internal IPs via default-allow-internal.
data "google_compute_subnetwork" "default" {
  name   = "default"
  region = var.region
}

module "client" {
  source           = "../modules/client"
  name_prefix      = var.name_prefix
  zone             = var.zone
  subnet_self_link = data.google_compute_subnetwork.default.self_link
  repo_url         = var.repo_url
  repo_ref         = var.repo_ref
  labels           = local.labels
}
