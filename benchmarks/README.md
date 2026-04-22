# Benchmarks

Latency harness for the session backends in this repository. Output is
captured as JSON (raw timings + environment fingerprint) with a companion
Markdown summary so runs are comparable across machines and over time.

## Quick run

```bash
# 1. Start Aerospike CE locally
docker run -d --name aerospike -p 3000-3002:3000-3002 \
    aerospike/aerospike-server:latest

# 2. Install the package and the aerospike client
pip install -e .

# 3. Run the harness
export AEROSPIKE_HOST=127.0.0.1
python benchmarks/session_latency.py --backend aerospike
python benchmarks/session_latency.py --backend aerospike-sharded
```

Each run writes `benchmarks/results/<timestamp>-<backend>.json` alongside a
`.md` summary. The `.md` file is safe to paste directly into
`docs/benchmark-results.md`.

## What it measures

One benchmarked iteration is one *agent turn*:

```python
items = await session.get_items(limit=20)
await session.add_items([user_msg, assistant_msg])
```

Both per-op timings (`get_items(limit=20)`, `add_items(2)`) and the
end-to-end turn latency are captured. The harness reports p50 / p95 / p99,
mean, min, max, and stdev for each.

Before measurement begins each variant:

1. Clears any stale record from a previous run.
2. Pre-loads `--history-depth` items to put the session into the desired
   state. History depths like `0,50,200,1000` simulate a spectrum from a
   fresh session to a long-running one.
3. Runs `--warmup` un-timed turns so JIT / connection-pool / TCP slow-start
   effects don't leak into the distribution.

## Axes

| Flag | Default | Notes |
|---|---|---|
| `--backend` | `aerospike` | Also `aerospike-sharded`. Factory table in the module accepts new backends without restructuring. |
| `--history-depth` | `0,50,200` | Comma-separated. Each value produces a separate variant in the output. |
| `--concurrency` | `1` | Comma-separated levels. For each value `C`, `C` parallel asyncio tasks each drive their own session against a shared Aerospike client. |
| `--user-size` | `512` | User message size in bytes (content field). |
| `--assistant-size` | `1024` | Assistant message size in bytes. |
| `--iterations` | `500` | Measured turns per variant after warmup. |
| `--warmup` | `50` | Discarded warm-up turns. |
| `--ttl` | *(namespace default)* | Session TTL in seconds. Aerospike CE's default `test` namespace refuses non-zero TTLs; point the harness at a namespace configured for TTLs before setting this. |

Each `(depth, concurrency)` pair produces one variant in the output, so
`--history-depth 0,50,200 --concurrency 1,8,64` writes nine variants.

Defaults are tuned for a quick laptop run (finishes in well under a minute
on a modern machine). For publishable numbers bump iterations to ~5,000
and widen the history-depth grid.

## A note on record size

Aerospike's default `write-block-size` caps a single record at 1 MiB. The
non-sharded `AerospikeSession` stores an entire conversation in one
record, so it has a hard feasibility budget:

    (depth + 2 * iterations) * (user_size + assistant_size) < ~1 MiB

When the harness's measured turns would violate that budget, the
`aerospike` backend rotates mid-run: it catches
`SessionRecordTooLargeError`, clears the session, re-preloads to the
configured depth, and retries the iteration. Rotation cost is deliberately
excluded from the reported timings. The output summary reports
`rotations` and `dropped iters` so reviewers can see how often this
happened.

`ShardedAerospikeSession` has no such ceiling — it overflows into
additional records transparently.

## Concurrency

`--concurrency C` fans out `C` asyncio tasks that share a single
Aerospike client (deliberately — one client per task would hide the
effect of the connection pool under load, which is the main thing
concurrent tests are supposed to exercise). Each task owns a unique
session, runs its own warmup, and produces its own `iterations` measured
turns.

Two extra metrics show up once `C > 1`:

- **Throughput** — total measured turns across all tasks divided by the
  wall-clock time of the parallel `gather()`. Warmup and rotation
  retries are excluded, so the number is apples-to-apples across
  variants with different rotation rates.
- **Per-task `turn` p50 distribution** — min / median / max / stdev of
  each task's own p50 turn latency. A tight distribution means the load
  is being served uniformly; a wide one means some tasks are being
  starved.

The headline p50 / p95 / p99 for a concurrent variant are computed over
the union of every task's measured turns, so each turn contributes one
sample regardless of which task produced it.

## Output format

The JSON file contains:

- `environment` — git SHA, Python version, host, CPU info, Aerospike client
  and server versions, openai-agents version.
- `config` — exact arguments passed to the run (including the list of
  `concurrencies`).
- `variants[]` — one entry per `(history_depth, concurrency)` pair. Each
  variant carries:
  - `backend`, `history_depth_before_bench`, `concurrency`.
  - `summary` — per-op OpStats (`p50_ms`, `p95_ms`, `p99_ms`, `mean_ms`,
    `min_ms`, `max_ms`, `stdev_ms`, `n`) plus a `per_task_turn_p50_ms`
    block describing fan-out fairness.
  - `throughput_turns_per_second`, `wall_clock_seconds`.
  - `per_task_summaries[]` — one entry per task with its rotation
    counts and its own turn distribution.
  - `raw_ms` — every timing in milliseconds (unioned across tasks) so
    the full distribution can be plotted without rerunning.

## What is **not** measured yet

Known gaps, addressed in later phases:

- **Cross-backend comparison.** Only the two Aerospike variants are
  exercised. A Phase 3 harness will run the same workload against
  `SQLiteSession`, `RedisSession`, and `SQLAlchemySession` for relative
  numbers with fair configuration and a common machine.
- **Realistic item content.** Messages are synthetic padded strings. A
  later variant will replay captured traces from real agent runs with
  mixed item shapes (tool calls, structured outputs, binary content).

## Reproducibility checklist

When publishing numbers externally, verify:

- The machine is otherwise idle (no CI jobs, no build processes, no
  browser with many tabs).
- CPU governor is set to `performance` on Linux, or the equivalent.
- Aerospike is running in the default development configuration; note any
  deviations (for example, changing `write-block-size`).
- `--iterations` is at least 2,000 for tight p99 confidence.
- The host is captured in the JSON environment block so reviewers can tell
  whether the run is laptop-noisy or dedicated-hardware credible.
