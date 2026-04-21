# Benchmark run 2026-04-21T23:12:46.783713+00:00

## Environment

- backend: `aerospike-sharded`
- git: `44f6f14645c2e34ab34935b118e43a137eab0bf2` (dirty: True)
- host: Linux 6.2.6-76060206-generic / x86_64 / x86_64 / 16 logical CPUs
- python: 3.10.12 (CPython)
- aerospike client: 19.2.1, server: 8.1.2.0
- openai-agents: 0.14.4

## Results

| backend | history depth | op | n | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |
|---|---:|---|---:|---:|---:|---:|---:|
| `aerospike-sharded` | 0 | get_items(limit=20) | 500 | 0.306 | 0.567 | 1.779 | 0.370 |
| `aerospike-sharded` | 0 | add_items(2) | 500 | 0.333 | 0.661 | 2.386 | 0.390 |
| `aerospike-sharded` | 0 | turn | 500 | 0.640 | 1.232 | 4.483 | 0.761 |
| `aerospike-sharded` | 50 | get_items(limit=20) | 500 | 0.302 | 0.506 | 1.526 | 0.354 |
| `aerospike-sharded` | 50 | add_items(2) | 500 | 0.321 | 0.599 | 1.558 | 0.377 |
| `aerospike-sharded` | 50 | turn | 500 | 0.623 | 1.110 | 3.375 | 0.732 |
| `aerospike-sharded` | 200 | get_items(limit=20) | 500 | 0.330 | 0.651 | 2.256 | 0.407 |
| `aerospike-sharded` | 200 | add_items(2) | 500 | 0.368 | 0.745 | 2.160 | 0.432 |
| `aerospike-sharded` | 200 | turn | 500 | 0.709 | 1.375 | 4.344 | 0.839 |
| `aerospike-sharded` | 1000 | get_items(limit=20) | 500 | 1.519 | 2.712 | 4.418 | 1.584 |
| `aerospike-sharded` | 1000 | add_items(2) | 500 | 0.630 | 1.277 | 2.138 | 0.739 |
| `aerospike-sharded` | 1000 | turn | 500 | 2.125 | 3.934 | 5.917 | 2.322 |
