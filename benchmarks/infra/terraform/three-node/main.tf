provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  labels = {
    project  = "openai-agents-aerospike"
    purpose  = "benchmark"
    topology = "three-node"
  }
}

resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "postgres_repl" {
  length  = 32
  special = false
}

module "network" {
  source           = "../modules/network"
  name_prefix      = var.name_prefix
  allowed_ssh_cidr = var.allowed_ssh_cidr
}

module "aerospike" {
  source             = "../modules/aerospike-cluster"
  name_prefix        = var.name_prefix
  node_count         = 3
  replication_factor = var.aerospike_replication_factor
  zone               = var.zone
  subnet_self_link   = module.network.subnet_self_link
  features_conf_path = var.aerospike_features_conf_path
  server_version     = var.aerospike_server_version
  labels             = local.labels
}

module "redis" {
  source           = "../modules/redis"
  name_prefix      = var.name_prefix
  topology         = "sentinel"
  zone             = var.zone
  subnet_self_link = module.network.subnet_self_link
  labels           = local.labels
}

module "postgres" {
  source               = "../modules/postgres"
  name_prefix          = var.name_prefix
  topology             = "replicated"
  zone                 = var.zone
  subnet_self_link     = module.network.subnet_self_link
  subnet_cidr          = module.network.subnet_cidr
  db_password          = random_password.postgres.result
  replication_password = random_password.postgres_repl.result
  labels               = local.labels
}

module "client" {
  source           = "../modules/client"
  name_prefix      = var.name_prefix
  zone             = var.zone
  subnet_self_link = module.network.subnet_self_link
  repo_url         = var.repo_url
  repo_ref         = var.repo_ref
  labels           = local.labels
}
