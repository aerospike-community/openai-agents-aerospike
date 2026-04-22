# Benchmark orchestrator

Two small scripts that turn an applied Terraform topology into a
reproducible, stamped benchmark result set.

## Prerequisites

* The matching Terraform root (`benchmarks/infra/terraform/single-node/`
  or `…/three-node/`) has been applied and has live GCP resources.
* `gcloud auth login` + `gcloud auth application-default login` done.
* `jq` and `gcloud` on your `$PATH`.
* You can SSH to the client VM (`gcloud compute ssh <client_name>` works).

## One-shot workflow

```bash
# 1. Stand up the infra (pick one).
cd benchmarks/infra/terraform/single-node
terraform init && terraform apply

# 2. Drive a sanity sweep (~5 min, catches wiring bugs cheaply).
cd ../../../..
./benchmarks/infra/orchestrator/run_sweep.sh single-node sanity

# 3. Summarize.
python benchmarks/infra/orchestrator/summarize_sweep.py \
    benchmarks/results/gcp/<timestamp>-single-node-sanity-headline/

# 4. If sanity looks good, run the full sweep (~3-4 hr).
./benchmarks/infra/orchestrator/run_sweep.sh single-node full

# 5. Tear down.
cd benchmarks/infra/terraform/single-node
terraform destroy
```

Repeat against `three-node` for the cluster comparison.

## Durability variants

The orchestrator itself does not reconfigure backend durability. To run
the durability sweep:

1. `terraform apply -var='postgres_synchronous_commit=off' -var='aerospike_commit_to_device=true'`
   (both vars exist in the postgres and aerospike-cluster modules —
   expose them in the root `variables.tf` if you want to drive them
   from tfvars).
2. `./run_sweep.sh <topology> full --tag=durability-relaxed-as-strict-pg`
3. Summarize and compare the `--tag=headline` vs durability output dirs.

## Output layout

```
benchmarks/results/gcp/
└── 20260421T153000Z-single-node-sanity-headline/
    ├── manifest.json      # what ran, commit, terraform outputs (redacted)
    ├── aerospike.json     # raw harness output per backend
    ├── redis.json
    ├── sqlalchemy.json
    └── SUMMARY.md         # written by summarize_sweep.py
```

Everything except `manifest.json` is a pass-through of the harness's
own output; the harness already handles multi-variant matrices within
one invocation. The orchestrator's only job is to drive one invocation
per backend, stamp the results directory with (topology, pass, tag),
and pull the files back.
