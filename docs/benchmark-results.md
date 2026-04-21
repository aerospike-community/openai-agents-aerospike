# Benchmark results

**Status:** Phase 1 (single-machine, laptop). These numbers are directionally
correct but not publication-grade; Phase 2 on dedicated hardware (likely GCP)
will replace them. The harness used to produce them lives in
[`benchmarks/session_latency.py`](../benchmarks/session_latency.py), and both
runs' raw JSON + per-run Markdown summaries are committed under
[`benchmarks/results/`](../benchmarks/results/).

## Methodology

- **Workload.** One "turn" is the unit of work the SDK's `Runner` performs
  per user input: `session.get_items(limit=20)` followed by
  `session.add_items([user_msg, assistant_msg])`. We time each op and the
  full turn separately.
- **Item shape.** 512-byte user message content + 1,024-byte assistant
  message content (the `--user-size` / `--assistant-size` defaults), a
  realistic short-chat profile.
- **History depths.** Sessions are pre-loaded with 0, 50, 200, or 1,000
  items before measurement starts to simulate everything from a fresh
  conversation to a long-running one.
- **Samples.** 50 warmup + 500 measured turns per (backend, depth)
  variant.
- **Stats.** `time.perf_counter` wall-clock, reported as p50 / p95 / p99 /
  mean / stdev. Raw per-iteration timings are preserved in the JSON output
  for re-plotting.
- **Record-size budget.** The non-sharded `AerospikeSession` stores an
  entire conversation in a single 1 MiB record. Runs that would exceed
  that budget rotate mid-run (clear + re-preload); rotation cost is
  deliberately excluded from the reported distributions. Neither Phase 1
  run triggered rotations.

### Environment

- Host: Linux 6.2.6 / x86_64 / 16 logical CPUs (laptop, otherwise idle
  during runs).
- Python 3.10.12 (CPython), aerospike client 19.2.1, Aerospike CE server
  8.1.2.0 in the default development configuration (`test` namespace,
  in-memory storage), running in Docker on the same host.
- `openai-agents` 0.14.4.

Network cost is effectively loopback, so these numbers overstate what
users will see against a real cluster across a network hop; expect
Phase 2 to land higher but still low-single-digit milliseconds at p50.

## Results

### `AerospikeSession` (single-record)

| history depth | op | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |
|---:|---|---:|---:|---:|---:|
| 0 | `get_items(limit=20)` | 0.292 | 0.444 | 1.066 | 0.322 |
| 0 | `add_items(2)` | 0.318 | 0.525 | 1.295 | 0.367 |
| 0 | **turn** | **0.614** | 0.954 | 2.987 | 0.689 |
| 50 | `get_items(limit=20)` | 0.260 | 0.469 | 1.844 | 0.321 |
| 50 | `add_items(2)` | 0.290 | 0.618 | 2.485 | 0.361 |
| 50 | **turn** | **0.553** | 1.096 | 3.844 | 0.682 |
| 200 | `get_items(limit=20)` | 0.276 | 0.565 | 2.040 | 0.340 |
| 200 | `add_items(2)` | 0.319 | 0.919 | 3.277 | 0.425 |
| 200 | **turn** | **0.595** | 1.486 | 4.926 | 0.764 |

Depth 1,000 is infeasible for this backend at the default item sizes
because 2,000 total items × ~1.5 KiB ≈ 3 MiB exceeds the 1 MiB
single-record cap. Use `ShardedAerospikeSession` (below) or compaction
for long-running sessions.

### `ShardedAerospikeSession` (transparent overflow)

| history depth | op | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |
|---:|---|---:|---:|---:|---:|
| 0 | `get_items(limit=20)` | 0.306 | 0.567 | 1.779 | 0.370 |
| 0 | `add_items(2)` | 0.333 | 0.661 | 2.386 | 0.390 |
| 0 | **turn** | **0.640** | 1.232 | 4.483 | 0.761 |
| 50 | `get_items(limit=20)` | 0.302 | 0.506 | 1.526 | 0.354 |
| 50 | `add_items(2)` | 0.321 | 0.599 | 1.558 | 0.377 |
| 50 | **turn** | **0.623** | 1.110 | 3.375 | 0.732 |
| 200 | `get_items(limit=20)` | 0.330 | 0.651 | 2.256 | 0.407 |
| 200 | `add_items(2)` | 0.368 | 0.745 | 2.160 | 0.432 |
| 200 | **turn** | **0.709** | 1.375 | 4.344 | 0.839 |
| 1000 | `get_items(limit=20)` | 1.519 | 2.712 | 4.418 | 1.584 |
| 1000 | `add_items(2)` | 0.630 | 1.277 | 2.138 | 0.739 |
| 1000 | **turn** | **2.125** | 3.934 | 5.917 | 2.322 |

## Observations

- **Sub-millisecond turns.** At practical history depths (up to ~200
  items), both backends deliver p50 agent turn latency well under one
  millisecond on this machine.
- **Sharding overhead is small at low depth.** `ShardedAerospikeSession`
  costs roughly 50-100 μs of p50 turn latency over the non-sharded
  variant, reflecting the extra shard-0 read for the `active_shard`
  pointer. p95 / p99 tails are similar.
- **Sharding scales where the non-sharded variant cannot.** At depth
  1,000 (roughly 1.5 MiB of conversation after warmup), the non-sharded
  `AerospikeSession` is simply infeasible; the sharded variant keeps p50
  turns at ~2.1 ms and p99 under 6 ms. `get_items(limit=20)` is the
  dominant cost at that depth because today it performs a `batch_read`
  across every shard and then concatenates. Reading only the latest
  shards when `limit` is small is the most obvious Phase 2 optimization.
- **Add-path is flat.** `add_items(2)` times are essentially independent
  of history depth for both backends because the server-side `operate`
  list-append op is O(1) in the existing list size.

## Caveats for these numbers

- Single machine, loopback network. Real deployments cross at least one
  network hop and usually a cluster of 3 or more nodes; Phase 2 will
  capture that.
- Single session driven by a single asyncio task. Aerospike's tail
  behavior under concurrent load is the number that matters most for
  real production workloads, and we don't measure it yet.
- In-memory storage engine. Phase 2 will include a persistent-storage
  run so durability cost is visible.
- No cross-backend comparison. `SQLiteSession`, `RedisSession`, and
  `SQLAlchemySession` will be run against the same harness in Phase 3.

## Reproducing

```bash
export AEROSPIKE_HOST=127.0.0.1
python benchmarks/session_latency.py --backend aerospike
python benchmarks/session_latency.py --backend aerospike-sharded \
    --history-depth 0,50,200,1000
```

See [`benchmarks/README.md`](../benchmarks/README.md) for the full set of
knobs and the reproducibility checklist.
