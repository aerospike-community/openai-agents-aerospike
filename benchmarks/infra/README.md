# Benchmark infrastructure

Terraform + startup scripts to stand up the GCP benchmark fleet in
either of two topologies. Workload matrix lives in
`internal/gcp-benchmark-decisions.md`; per-backend configuration lives
in `tuning-profile.md` next to this file.

## Two topologies

The comparison story is only complete if we run the same workload
against each backend at two distinct shapes: a single-node install
(per-node ceiling) and a 3-node production-shape cluster (how each
backend's architecture scales horizontally). The two Terraform roots
are designed to be applied **one at a time** — tear one down before
bringing the next one up, because the single-node workload generator
is reused across both.

### `single-node/` (1x Aerospike + 1x Redis + 1x Postgres + 1x client)

| Role | Count | Storage |
|---|---:|---|
| Aerospike (no replication, `replication-factor 1`) | 1 | 1x 375 GB local NVMe, partitioned into 4 devices |
| Redis (standalone) | 1 | 1x 375 GB local NVMe mounted at `/var/lib/redis` (AOF everysec) |
| Postgres (standalone) | 1 | 1x 375 GB local NVMe mounted at the data dir |
| Client | 1 | 50 GB `pd-balanced` |

Resources created: **10** (3 DB VMs + 1 client + VPC/subnet + 2 firewalls + 2 Postgres passwords).

### `three-node/` (3x Aerospike + 3x Redis Sentinel + 3x Postgres primary/replica + 1x client)

| Role | Count | Topology | Storage |
|---|---:|---|---|
| Aerospike | 3 | Native cluster, RF=2, sharded | 1x 375 GB local NVMe per node, partitioned into 4 devices |
| Redis | 3 | 1 primary + 2 replicas, every node also runs `redis-sentinel` (quorum 2) | 1x 375 GB local NVMe per node |
| Postgres | 3 | Primary + 2 async streaming replicas | 1x 375 GB local NVMe per node |
| Client | 1 | — | 50 GB `pd-balanced` |

Resources created: **16** (9 DB VMs + 1 client + VPC/subnet + 2 firewalls + 2 passwords).

All VMs are `n2d-standard-8` in a single zone (`us-central1-a`). Client
and DB nodes share one VPC so RTT is 100-300 us.

## Prerequisites

1. A fresh GCP project with billing attached and a budget alert.
2. `gcloud auth application-default login` against that project (stores
   credentials in `~/.config/gcloud/application_default_credentials.json`;
   Terraform's Google provider reads them automatically).
3. `gcloud config set project <YOUR_PROJECT_ID>`.
4. Aerospike Enterprise feature-key file at `internal/features.conf`.
   `internal/` is gitignored; the tfvars examples point at this path.
5. Terraform 1.5+.

## Run — single-node first

```bash
cd benchmarks/infra/terraform/single-node
cp terraform.tfvars.example terraform.tfvars
# fill in project_id and allowed_ssh_cidr (use `curl -s ifconfig.me`)
terraform init
terraform apply
```

Harvest connection details:

```bash
terraform output -raw redis_url                   # redis://<ip>:6379/0
terraform output -raw sqlalchemy_url              # Postgres URL (sensitive)
terraform output -raw aerospike_seed_ip           # AEROSPIKE_HOST value
terraform output -raw client_name                 # VM name for gcloud ssh
```

Drive the sweep from the client VM:

```bash
gcloud compute ssh "$(terraform output -raw client_name)" --zone=us-central1-a
# inside the VM:
cd aerospike-openai-agents && . .venv/bin/activate
python benchmarks/session_latency.py --backend aerospike \
  --aerospike-host "$AEROSPIKE_SEED_IP" \
  --concurrency 1,4,16,64 --history-depth 0,50 \
  --iterations 500 --warmup 50
```

Tear down when the single-node sweep is done:

```bash
terraform destroy
```

## Run — three-node next

```bash
cd benchmarks/infra/terraform/three-node
cp terraform.tfvars.example terraform.tfvars
# fill in, then:
terraform init
terraform apply
```

Same workflow, different `aerospike_seed_ip` / `redis_url` / `sqlalchemy_url`.

Tear down when done — local NVMe data goes with the instance, so
`destroy` is the only cleanup step needed.

## Directory layout

```
benchmarks/infra/
├── README.md                 # this file
├── tuning-profile.md         # per-backend configuration, authoritative
└── terraform/
    ├── modules/
    │   ├── network/          # VPC, subnet, firewall
    │   ├── aerospike-cluster/# N-node Aerospike (N-partitioned NVMe)
    │   ├── redis/            # standalone or sentinel (3 nodes colocated)
    │   ├── postgres/         # standalone or primary + 2 async replicas
    │   └── client/           # load generator
    ├── single-node/          # 1x each root
    └── three-node/           # 3x each root (Sentinel + replicated Postgres)
```

Modules are shared; the roots differ only in which topology variable
they pass and which count.

## Status

Startup scripts now apply `tuning-profile.md` — NVMe formatting /
partitioning, OS baseline (THP off, governor, sysctl), per-backend
tuning knobs. Runs against this fleet are comparison-grade once the
tuning profile PR is merged.
