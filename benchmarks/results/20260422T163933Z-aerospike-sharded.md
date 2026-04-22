# Benchmark run 2026-04-22T16:39:02.072321+00:00

## Environment

- backend: `aerospike-sharded`
- git: `112de08b39d1ff15d2be49cd4cb435bec05e4557` (dirty: True)
- host: Linux 6.2.6-76060206-generic / x86_64 / x86_64 / 16 logical CPUs
- python: 3.10.12 (CPython)
- aerospike client: 19.2.1, server: 8.1.2.0
- openai-agents: 0.14.4

## Results

| backend | depth | C | op | n | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| `aerospike-sharded` | 0 | 1 | get_items(limit=20) | 200 | 0.294 | 0.422 | 0.497 | 0.307 |
| `aerospike-sharded` | 0 | 1 | add_items(2) | 200 | 0.349 | 0.584 | 0.684 | 0.367 |
| `aerospike-sharded` | 0 | 1 | turn | 200 | 0.643 | 1.007 | 1.187 | 0.674 |
| `aerospike-sharded` | 0 | 2 | get_items(limit=20) | 400 | 0.358 | 0.533 | 0.591 | 0.373 |
| `aerospike-sharded` | 0 | 2 | add_items(2) | 400 | 0.521 | 0.697 | 0.809 | 0.500 |
| `aerospike-sharded` | 0 | 2 | turn | 400 | 0.881 | 1.148 | 1.406 | 0.872 |
| `aerospike-sharded` | 0 | 4 | get_items(limit=20) | 800 | 0.711 | 1.039 | 1.151 | 0.726 |
| `aerospike-sharded` | 0 | 4 | add_items(2) | 800 | 0.807 | 1.216 | 1.404 | 0.825 |
| `aerospike-sharded` | 0 | 4 | turn | 800 | 1.523 | 2.064 | 2.343 | 1.551 |
| `aerospike-sharded` | 0 | 8 | get_items(limit=20) | 1600 | 1.127 | 1.758 | 2.093 | 1.072 |
| `aerospike-sharded` | 0 | 8 | add_items(2) | 1600 | 1.107 | 1.802 | 2.171 | 1.085 |
| `aerospike-sharded` | 0 | 8 | turn | 1600 | 2.341 | 3.318 | 3.862 | 2.156 |

## Throughput

Measured turns (across all tasks) divided by the wall-clock time of the
parallel gather(). Warmup and rotation retries do not contribute.

| backend | depth | C | throughput (turns/s) | wall (s) |
|---|---:|---:|---:|---:|
| `aerospike-sharded` | 0 | 1 | 1325.5 | 0.151 |
| `aerospike-sharded` | 0 | 2 | 2038.2 | 0.196 |
| `aerospike-sharded` | 0 | 4 | 2296.9 | 0.348 |
| `aerospike-sharded` | 0 | 8 | 157.8 | 10.142 |

## Fairness (per-task turn p50 distribution)

A uniformly fast run has a tight per-task p50 distribution; a large gap
between min and max indicates one or more tasks are starving.

| backend | depth | C | tasks | min p50 (ms) | p50 p50 (ms) | max p50 (ms) | stdev (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `aerospike-sharded` | 0 | 2 | 2 | 0.875 | 0.881 | 0.886 | 0.005 |
| `aerospike-sharded` | 0 | 4 | 4 | 1.513 | 1.523 | 1.525 | 0.005 |
| `aerospike-sharded` | 0 | 8 | 8 | 2.300 | 2.333 | 2.389 | 0.031 |

## Device-overload retries

Aerospike nodes raise `DeviceOverload` when their write queue to persistent storage can't keep up. The harness backs off with jittered exponential delay and retries the same iteration; the retried attempts are excluded from the distributions above.

| backend | depth | C | overload retries |
|---|---:|---:|---:|
| `aerospike-sharded` | 0 | 8 | 329 |
