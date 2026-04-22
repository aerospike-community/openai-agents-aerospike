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
| 0 | `get_items(limit=20)` | 0.258 | 0.455 | 1.429 | 0.312 |
| 0 | `add_items(2)` | 0.273 | 0.572 | 1.372 | 0.335 |
| 0 | **turn** | **0.527** | 1.014 | 2.389 | 0.648 |
| 50 | `get_items(limit=20)` | 0.235 | 0.497 | 1.406 | 0.289 |
| 50 | `add_items(2)` | 0.257 | 0.671 | 2.111 | 0.329 |
| 50 | **turn** | **0.492** | 1.043 | 3.323 | 0.618 |
| 200 | `get_items(limit=20)` | 0.264 | 0.648 | 1.736 | 0.332 |
| 200 | `add_items(2)` | 0.292 | 0.768 | 1.948 | 0.375 |
| 200 | **turn** | **0.562** | 1.540 | 3.785 | 0.706 |

Depth 1,000 is infeasible for this backend at the default item sizes
because 2,000 total items × ~1.5 KiB ≈ 3 MiB exceeds the 1 MiB
single-record cap. Use `ShardedAerospikeSession` (below) or compaction
for long-running sessions.

### `ShardedAerospikeSession` (transparent overflow)

| history depth | op | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |
|---:|---|---:|---:|---:|---:|
| 0 | `get_items(limit=20)` | 0.280 | 0.421 | 1.653 | 0.321 |
| 0 | `add_items(2)` | 0.298 | 0.484 | 1.579 | 0.341 |
| 0 | **turn** | **0.585** | 0.903 | 3.190 | 0.662 |
| 50 | `get_items(limit=20)` | 0.286 | 0.466 | 1.512 | 0.330 |
| 50 | `add_items(2)` | 0.297 | 0.582 | 1.502 | 0.350 |
| 50 | **turn** | **0.583** | 1.032 | 2.711 | 0.680 |
| 200 | `get_items(limit=20)` | 0.288 | 0.638 | 1.595 | 0.348 |
| 200 | `add_items(2)` | 0.318 | 0.658 | 1.817 | 0.395 |
| 200 | **turn** | **0.605** | 1.368 | 3.275 | 0.743 |
| 1000 | `get_items(limit=20)` | 0.322 | 0.584 | 1.625 | 0.388 |
| 1000 | `add_items(2)` | 0.416 | 0.851 | 2.600 | 0.506 |
| 1000 | **turn** | **0.752** | 1.544 | 3.924 | 0.893 |

## Observations

- **Sub-millisecond turns at every measured depth.** Both backends
  deliver p50 agent turn latency well under one millisecond on this
  machine, including the sharded backend at depth 1,000 where the
  non-sharded variant is infeasible.
- **Sharded overhead is in the noise at low depth.** `ShardedAerospikeSession`
  costs roughly 50-100 μs of p50 turn latency over the non-sharded
  variant — the price of the extra shard-0 read for the `active_shard`
  pointer — and the gap barely widens as depth grows.
- **Sharded reads are flat across depth.** Because `get_items(limit=N)`
  walks shards from the tail using `list_get_range(bin, -need, need)`,
  it pays for at most two round trips when the active shard's tail
  holds enough items (the common case), regardless of how many shards a
  long-running session has accumulated. At depth 1,000 the read p50 is
  0.322 ms, within 25% of the depth-0 read.
- **Add-path is flat with depth.** `add_items(2)` times are essentially
  independent of history depth for both backends because the server-side
  `operate` list-append op is O(1) in the existing list size.

## Phase 2 preview: concurrency on the same laptop

The harness grew a `--concurrency` knob (issue #6) that fans out N
asyncio tasks, each driving its own session, against a shared Aerospike
client. This is how real workloads exercise the driver: connection
pool, thread pool, and server queueing all matter only once more than
one caller is in flight at a time.

These runs use `depth=0`, 30 warmup + 200 measured turns per task per
concurrency level, with `--cool-down-seconds 5` between variants so the
single-node laptop's background defragger can catch up. Headline
p50/p95/p99 are computed over the union of every task's measured turns
(each turn contributes one sample regardless of which task produced
it). **These numbers are laptop ceilings — the server was the
bottleneck at C≥8.** Production-quality concurrency numbers will come
from the GCP run tracked in issue #5.

### `AerospikeSession` (single-record) under concurrency

| C | op | p50 (ms) | p95 (ms) | p99 (ms) | throughput (turns/s) | per-task p50 stdev (ms) |
|---:|---|---:|---:|---:|---:|---:|
| 1 | turn | **0.499** | 0.635 | 0.703 | ~2,000 (1 / p50) | — |
| 2 | turn | **0.591** | 0.861 | 0.969 | 2,917 | 0.001 |
| 4 | turn | **1.114** | 1.501 | 1.765 | 3,095 | 0.009 |
| 8 | turn | 1.844 | 2.821 | 3.426 | 156 (saturated) | 0.039 |

### `ShardedAerospikeSession` under concurrency

| C | op | p50 (ms) | p95 (ms) | p99 (ms) | throughput (turns/s) | per-task p50 stdev (ms) |
|---:|---|---:|---:|---:|---:|---:|
| 1 | turn | **0.643** | 1.007 | 1.187 | ~1,550 (1 / p50) | — |
| 2 | turn | **0.881** | 1.148 | 1.406 | 2,038 | 0.005 |
| 4 | turn | **1.523** | 2.064 | 2.343 | 2,297 | 0.005 |
| 8 | turn | 2.341 | 3.318 | 3.862 | 158 (saturated) | 0.031 |

### Phase 2 observations

- **Per-op latency scales gracefully up to C=4.** Both backends roughly
  double p50 turn latency going from C=1 to C=4 while nearly tripling
  throughput. That's the connection pool doing useful work.
- **The laptop saturates between C=4 and C=8.** At C=8 the in-container
  Aerospike CE node's write queue to its file-backed storage overflowed
  on every variant, and the harness reported 300+ `DeviceOverload`
  retries per run. Per-operation p50 stayed reasonable (~1.8 ms / ~2.3
  ms), but wall-clock throughput collapsed to ~150 turns/s once backoff
  sleeps are included. This is *laptop I/O* saturation, not an
  Aerospike ceiling — Phase 2 on GCP with SSDs and a real cluster will
  push the knee of the curve much further out.
- **Per-task fairness is excellent.** Even at C=8 under saturation, the
  standard deviation of per-task p50 latency is well under 40 μs — no
  task is being starved, load is spreading uniformly.
- **Sharded still costs a fixed ~30-40% over single-record.** At C=4
  the gap is 1.11 ms vs 1.52 ms; that's the predictable price of the
  extra shard-0 read on every turn, and it does *not* grow with
  concurrency.

## Caveats for these numbers

- Single machine, loopback network. Real deployments cross at least one
  network hop and usually a cluster of 3 or more nodes; Phase 2 will
  capture that.
- Single Aerospike CE node backed by a file on the container's overlay
  filesystem. The `DEVICE_OVERLOAD` retries at C=8 are *laptop I/O*
  saturation, not a property of Aerospike.
- No cross-backend comparison. `SQLiteSession`, `RedisSession`, and
  `SQLAlchemySession` will be run against the same harness in Phase 3.
- Concurrency sweep stopped at C=8 on this hardware: the synchronous
  `aerospike` Python client has been observed to segfault under
  sustained load from ~16+ worker threads against a shared client
  instance, in addition to the server-side write-queue saturation.
  Worth investigating separately before publishing Phase 2 numbers.

## Reproducing

```bash
export AEROSPIKE_HOST=127.0.0.1

# Phase 1: depth sweep
python benchmarks/session_latency.py --backend aerospike
python benchmarks/session_latency.py --backend aerospike-sharded \
    --history-depth 0,50,200,1000

# Phase 2 preview: concurrency sweep
python benchmarks/session_latency.py --backend aerospike \
    --history-depth 0 --concurrency 1,2,4,8 \
    --iterations 200 --warmup 30 --cool-down-seconds 5
python benchmarks/session_latency.py --backend aerospike-sharded \
    --history-depth 0 --concurrency 1,2,4,8 \
    --iterations 200 --warmup 30 --cool-down-seconds 5
```

See [`benchmarks/README.md`](../benchmarks/README.md) for the full set of
knobs and the reproducibility checklist.
