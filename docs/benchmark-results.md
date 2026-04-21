# Benchmark results

**Status:** pending. The benchmark harness lives in [`benchmarks/session_latency.py`](../benchmarks/session_latency.py) and targets `AerospikeSession`. Numbers will be posted here once the harness has been run in a controlled environment.

## Methodology

- Single-machine setup: the database runs in Docker on the same host as the benchmark, to minimize network variability.
- Measurements are wall-clock `time.perf_counter` deltas across 1,000 iterations per op after a 100-iteration warm-up.
- Three hot-path ops: `add_items(1 item)`, `get_items(limit=20)`, `pop_item()`.
- Reported statistics: p50, p95, p99, mean.

## Raw harness output

```
pending benchmark run
```

## Analysis

Pending benchmark execution. The primary metric of interest is **p99 latency under concurrent load**, which is the tightest observable signal on whether the single-record + `operate()` design holds up when many sessions are live at once.

Comparisons against other SDK-supported session backends (`SQLiteSession`, `RedisSession`, `SQLAlchemySession`) are a follow-up item. They will be run on the same machine and against fresh containers so the setup is reproducible from [`benchmarks/session_latency.py`](../benchmarks/session_latency.py).
