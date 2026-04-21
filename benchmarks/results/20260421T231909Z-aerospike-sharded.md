# Benchmark run 2026-04-21T23:19:07.646087+00:00

## Environment

- backend: `aerospike-sharded`
- git: `dc647b1535e1aca6ead59e52753a18314322d418` (dirty: True)
- host: Linux 6.2.6-76060206-generic / x86_64 / x86_64 / 16 logical CPUs
- python: 3.10.12 (CPython)
- aerospike client: 19.2.1, server: 8.1.2.0
- openai-agents: 0.14.4

## Results

| backend | history depth | op | n | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |
|---|---:|---|---:|---:|---:|---:|---:|
| `aerospike-sharded` | 0 | get_items(limit=20) | 500 | 0.280 | 0.421 | 1.653 | 0.321 |
| `aerospike-sharded` | 0 | add_items(2) | 500 | 0.298 | 0.484 | 1.579 | 0.341 |
| `aerospike-sharded` | 0 | turn | 500 | 0.585 | 0.903 | 3.190 | 0.662 |
| `aerospike-sharded` | 50 | get_items(limit=20) | 500 | 0.286 | 0.466 | 1.512 | 0.330 |
| `aerospike-sharded` | 50 | add_items(2) | 500 | 0.297 | 0.582 | 1.502 | 0.350 |
| `aerospike-sharded` | 50 | turn | 500 | 0.583 | 1.032 | 2.711 | 0.680 |
| `aerospike-sharded` | 200 | get_items(limit=20) | 500 | 0.288 | 0.638 | 1.595 | 0.348 |
| `aerospike-sharded` | 200 | add_items(2) | 500 | 0.318 | 0.658 | 1.817 | 0.395 |
| `aerospike-sharded` | 200 | turn | 500 | 0.605 | 1.368 | 3.275 | 0.743 |
| `aerospike-sharded` | 1000 | get_items(limit=20) | 500 | 0.322 | 0.584 | 1.625 | 0.388 |
| `aerospike-sharded` | 1000 | add_items(2) | 500 | 0.416 | 0.851 | 2.600 | 0.506 |
| `aerospike-sharded` | 1000 | turn | 500 | 0.752 | 1.544 | 3.924 | 0.893 |
