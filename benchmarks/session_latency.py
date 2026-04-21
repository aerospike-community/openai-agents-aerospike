"""Session latency benchmark (scaffold).

Compares ``AerospikeSession`` against the upstream built-in sessions on the
same machine. Reports p50/p95/p99 latency for the three hot-path operations:

- ``add_items`` (single-item append)
- ``get_items`` (bounded retrieval)
- ``pop_item`` (atomic tail removal)

The scaffold here runs only the ``AerospikeSession`` leg. Side-by-side
comparisons against the other SDK-supported session backends are a
follow-up item and will be added to the same harness.

Run::

    AEROSPIKE_HOST=127.0.0.1 python benchmarks/session_latency.py \\
        --iterations 1000 --session-id bench-1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from typing import Any

import aerospike

from openai_agents_aerospike import AerospikeSession


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    return statistics.quantiles(values, n=100)[int(pct) - 1] if len(values) > 1 else values[0]


async def bench_add_items(session: AerospikeSession, iterations: int) -> list[float]:
    timings: list[float] = []
    for i in range(iterations):
        t0 = time.perf_counter()
        await session.add_items([{"role": "user", "content": f"msg-{i}"}])
        timings.append((time.perf_counter() - t0) * 1000)
    return timings


async def bench_get_items(session: AerospikeSession, iterations: int) -> list[float]:
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        await session.get_items(limit=20)
        timings.append((time.perf_counter() - t0) * 1000)
    return timings


async def bench_pop_item(session: AerospikeSession, iterations: int) -> list[float]:
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        await session.pop_item()
        timings.append((time.perf_counter() - t0) * 1000)
    return timings


def summarize(name: str, timings: list[float]) -> dict[str, Any]:
    return {
        "op": name,
        "n": len(timings),
        "p50_ms": percentile(timings, 50),
        "p95_ms": percentile(timings, 95),
        "p99_ms": percentile(timings, 99),
        "mean_ms": statistics.fmean(timings) if timings else float("nan"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--session-id", default="benchmark")
    args = parser.parse_args()

    client = aerospike.client(
        {"hosts": [(os.environ.get("AEROSPIKE_HOST", "127.0.0.1"), 3000)]},
    ).connect()

    session = AerospikeSession(
        session_id=args.session_id,
        client=client,
        namespace="test",
        set_name="benchmark",
    )

    try:
        await session.clear_session()

        add_t = await bench_add_items(session, args.iterations)
        get_t = await bench_get_items(session, args.iterations)
        pop_t = await bench_pop_item(session, args.iterations)

        for summary in (
            summarize("add_items", add_t),
            summarize("get_items(limit=20)", get_t),
            summarize("pop_item", pop_t),
        ):
            print(
                f"{summary['op']:<22} "
                f"n={summary['n']:<5} "
                f"p50={summary['p50_ms']:.3f}ms "
                f"p95={summary['p95_ms']:.3f}ms "
                f"p99={summary['p99_ms']:.3f}ms "
                f"mean={summary['mean_ms']:.3f}ms"
            )
    finally:
        await session.clear_session()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
