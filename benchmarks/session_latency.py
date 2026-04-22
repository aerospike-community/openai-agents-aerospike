"""Session latency benchmark harness.

Measures end-to-end agent-turn latency for the session backends in this
repository. A *turn* is the unit of work the SDK's ``Runner`` performs per
user input::

    get_items(limit=20)             # load the recent conversation
    add_items([user_msg, assistant_msg])   # persist the new exchange

Both per-op and full-turn timings are captured at p50 / p95 / p99 / mean,
across a configurable grid of history depths (how many items were already
in the session), item sizes, and concurrency levels (how many parallel
sessions are driving the cluster simultaneously). Raw timings, the
summary, and an environment fingerprint are written as a single JSON
file so downstream analysis tools can plot distributions or compare runs.

Run::

    # Start an Aerospike CE server locally
    docker run -d --name aerospike -p 3000-3002:3000-3002 \\
        aerospike/aerospike-server:latest

    AEROSPIKE_HOST=127.0.0.1 python benchmarks/session_latency.py \\
        --backend aerospike \\
        --history-depth 0,50,200 \\
        --concurrency 1,8,64 \\
        --iterations 500 --warmup 50

Output lands in ``benchmarks/results/`` by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aerospike

from openai_agents_aerospike import (
    AerospikeSession,
    SessionRecordTooLargeError,
    ShardedAerospikeSession,
)

# Transient server-side throttling. An Aerospike node raises this when its
# write queue to persistent storage can't keep up. Real deployments see
# this under write bursts; the harness treats it as retryable with a
# jittered backoff rather than aborting the run.
_DEVICE_OVERLOAD = getattr(getattr(aerospike, "exception", None), "DeviceOverload", None)

# Hard cap on consecutive retries per iteration. Each retry sleeps with
# jittered exponential backoff capped at 250 ms, so this bound translates
# to roughly a one-minute patience window before the harness concludes
# the cluster is sustained-overloaded and surfaces the exception.
_MAX_OVERLOAD_RETRIES_PER_ITER = 500

_BACKEND_FACTORIES: dict[str, Callable[..., AerospikeSession]] = {
    "aerospike": AerospikeSession,
    "aerospike-sharded": ShardedAerospikeSession,
}


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


@dataclass
class OpStats:
    """Summary statistics for a single op's latency distribution (milliseconds)."""

    op: str
    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    stdev_ms: float


def _percentile(values: list[float], pct: float) -> float:
    """Return the ``pct``-th percentile of ``values`` in-order (1-100)."""
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    # statistics.quantiles(n=100) returns cut points, not including 0 / 100.
    # Index pct-1 gives the pct-th percentile.
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


def _summarize(op: str, timings_ms: list[float]) -> OpStats:
    if not timings_ms:
        return OpStats(op, 0, *([float("nan")] * 7))
    return OpStats(
        op=op,
        n=len(timings_ms),
        p50_ms=_percentile(timings_ms, 50),
        p95_ms=_percentile(timings_ms, 95),
        p99_ms=_percentile(timings_ms, 99),
        mean_ms=statistics.fmean(timings_ms),
        min_ms=min(timings_ms),
        max_ms=max(timings_ms),
        stdev_ms=statistics.pstdev(timings_ms) if len(timings_ms) > 1 else 0.0,
    )


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------


def _build_message(role: str, size_bytes: int, seq: int) -> dict[str, str]:
    """Build a chat-style item whose content is ``size_bytes`` bytes long.

    Content is made distinct per message (via ``seq``) so JSON compression
    heuristics can't collapse everything to a trivial repeat.
    """
    # Leave a little headroom for the seq prefix so final length ~ size_bytes.
    prefix = f"[{seq:08d}]"
    pad = max(0, size_bytes - len(prefix))
    return {"role": role, "content": prefix + ("x" * pad)}


async def _call_with_overload_backoff(coro_factory: Callable[[], Any]) -> Any:
    """Await ``coro_factory()`` with jittered retry on `DeviceOverload`.

    ``coro_factory`` is called fresh each retry — a coroutine object can
    only be awaited once.
    """
    attempts = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:
            if _DEVICE_OVERLOAD is not None and isinstance(exc, _DEVICE_OVERLOAD):
                attempts += 1
                if attempts > _MAX_OVERLOAD_RETRIES_PER_ITER:
                    raise
                backoff = min(0.25, 0.005 * (2 ** min(attempts, 6)))
                await asyncio.sleep(backoff * (0.5 + random.random()))
                continue
            raise


async def _preload_session(
    session: AerospikeSession,
    *,
    depth: int,
    user_size: int,
    assistant_size: int,
) -> None:
    """Seed ``session`` with ``depth`` alternating user/assistant items."""
    if depth <= 0:
        return
    # Insert in chunks of 10 so we don't build one monster list client-side.
    chunk = 10
    for base in range(0, depth, chunk):
        items: list[dict[str, str]] = []
        for i in range(base, min(base + chunk, depth)):
            if i % 2 == 0:
                items.append(_build_message("user", user_size, i))
            else:
                items.append(_build_message("assistant", assistant_size, i))

        async def _add(payload: list[Any] = items) -> None:
            await session.add_items(payload)

        await _call_with_overload_backoff(_add)


@dataclass
class _TaskResult:
    """Per-task timings and bookkeeping from one parallel worker."""

    task_id: int
    get_ms: list[float]
    add_ms: list[float]
    turn_ms: list[float]
    rotations: int
    retries_dropped: int
    overload_retries: int


async def _run_one_task(
    *,
    task_id: int,
    backend: str,
    factory: Callable[..., AerospikeSession],
    client: Any,
    depth: int,
    user_size: int,
    assistant_size: int,
    warmup: int,
    iterations: int,
    ttl: int | None,
) -> _TaskResult:
    """Drive a single session through warmup + measurement.

    A *task* is one session being exercised by one asyncio task against the
    shared Aerospike client. When ``concurrency > 1``, many of these run
    concurrently (see :func:`_run_one_variant`) so the distributions
    aggregate the experience of parallel sessions sharing one client
    connection pool.
    """
    session_id = f"bench-{backend}-{depth}-t{task_id}-{uuid.uuid4().hex[:8]}"
    session_kwargs: dict[str, Any] = {
        "session_id": session_id,
        "client": client,
        "namespace": os.environ.get("AEROSPIKE_NAMESPACE", "test"),
        "set_name": "benchmark",
    }
    if ttl is not None:
        session_kwargs["ttl"] = ttl
    session = factory(**session_kwargs)

    # Ensure a fresh record even if a prior aborted run left something behind.
    await _call_with_overload_backoff(session.clear_session)
    await _preload_session(session, depth=depth, user_size=user_size, assistant_size=assistant_size)

    get_ms: list[float] = []
    add_ms: list[float] = []
    turn_ms: list[float] = []
    rotations = 0
    retries_dropped = 0
    overload_retries = 0

    async def _rotate() -> None:
        nonlocal rotations
        rotations += 1
        await _call_with_overload_backoff(session.clear_session)
        await _preload_session(
            session, depth=depth, user_size=user_size, assistant_size=assistant_size
        )

    try:
        total_iters = warmup + iterations
        i = 0
        iter_overload_retries = 0
        while i < total_iters:
            user_seq = depth + 2 * i
            assistant_seq = user_seq + 1

            turn_items: list[Any] = [
                _build_message("user", user_size, user_seq),
                _build_message("assistant", assistant_size, assistant_seq),
            ]

            t0 = time.perf_counter()
            try:
                await session.get_items(limit=20)
                t1 = time.perf_counter()
                await session.add_items(turn_items)
                t2 = time.perf_counter()
            except SessionRecordTooLargeError:
                # Non-sharded backend hit the 1 MiB cap. Reset and retry the
                # same iteration slot so we still collect `iterations`
                # measured turns.
                retries_dropped += 1
                await _rotate()
                iter_overload_retries = 0
                continue
            except Exception as exc:
                # Transient write throttling from the server. Back off and
                # retry the same iteration. The failed op's timing is
                # discarded; only cleanly completed turns feed the
                # distribution.
                if _DEVICE_OVERLOAD is not None and isinstance(exc, _DEVICE_OVERLOAD):
                    overload_retries += 1
                    iter_overload_retries += 1
                    if iter_overload_retries > _MAX_OVERLOAD_RETRIES_PER_ITER:
                        raise
                    # Jittered exponential backoff, capped at 250 ms.
                    backoff = min(0.25, 0.005 * (2 ** min(iter_overload_retries, 6)))
                    await asyncio.sleep(backoff * (0.5 + random.random()))
                    continue
                raise

            iter_overload_retries = 0

            if i >= warmup:
                get_ms.append((t1 - t0) * 1000.0)
                add_ms.append((t2 - t1) * 1000.0)
                turn_ms.append((t2 - t0) * 1000.0)
            i += 1
    finally:
        # Task-owned cleanup only; the shared client lives at variant scope.
        try:
            await session.clear_session()
        except Exception:
            pass
        await session.close()

    return _TaskResult(
        task_id=task_id,
        get_ms=get_ms,
        add_ms=add_ms,
        turn_ms=turn_ms,
        rotations=rotations,
        retries_dropped=retries_dropped,
        overload_retries=overload_retries,
    )


async def _run_one_variant(
    *,
    backend: str,
    factory: Callable[..., AerospikeSession],
    client: Any,
    depth: int,
    concurrency: int,
    user_size: int,
    assistant_size: int,
    warmup: int,
    iterations: int,
    ttl: int | None,
) -> dict[str, Any]:
    """Run one (backend, depth, concurrency) variant and return summaries.

    ``concurrency`` tasks share a single Aerospike client (that's the
    connection pool we're trying to exercise — one client per task would
    hide whatever the pool does under load). Each task gets its own
    session, warms up independently, and is measured for ``iterations``
    turns. Per-task timings are unioned into a single distribution for
    the headline p50/p95/p99, and per-task summaries are preserved in
    the output so fan-out effects (one slow task vs. uniformly slow) are
    visible without re-running the bench.
    """
    tasks = [
        _run_one_task(
            task_id=t,
            backend=backend,
            factory=factory,
            client=client,
            depth=depth,
            user_size=user_size,
            assistant_size=assistant_size,
            warmup=warmup,
            iterations=iterations,
            ttl=ttl,
        )
        for t in range(concurrency)
    ]

    wall_t0 = time.perf_counter()
    task_results: list[_TaskResult] = await asyncio.gather(*tasks)
    wall_t1 = time.perf_counter()
    wall_seconds = wall_t1 - wall_t0

    # Aggregate across tasks: the headline distributions treat every
    # measured turn as a single sample, regardless of which task produced it.
    all_get = [t for tr in task_results for t in tr.get_ms]
    all_add = [t for tr in task_results for t in tr.add_ms]
    all_turn = [t for tr in task_results for t in tr.turn_ms]

    # Fairness indicator: distribution of per-task p50 turn latencies.
    per_task_turn_p50 = [_percentile(tr.turn_ms, 50) for tr in task_results if tr.turn_ms]

    total_rotations = sum(tr.rotations for tr in task_results)
    total_dropped = sum(tr.retries_dropped for tr in task_results)
    total_overload_retries = sum(tr.overload_retries for tr in task_results)

    # Throughput: total measured turns across all tasks divided by the
    # wall-clock time gather() took. Counts only measurement (not warmup
    # or rotation retries) so the number is apples-to-apples across
    # variants with different rotation rates.
    throughput_tps = len(all_turn) / wall_seconds if wall_seconds > 0 else 0.0

    return {
        "backend": backend,
        "history_depth_before_bench": depth,
        "concurrency": concurrency,
        "warmup": warmup,
        "iterations": iterations,
        "user_size_bytes": user_size,
        "assistant_size_bytes": assistant_size,
        "rotations": total_rotations,
        "retries_dropped": total_dropped,
        "overload_retries": total_overload_retries,
        "wall_clock_seconds": wall_seconds,
        "throughput_turns_per_second": throughput_tps,
        "summary": {
            "get_items_limit_20": asdict(_summarize("get_items(limit=20)", all_get)),
            "add_items_2": asdict(_summarize("add_items(2)", all_add)),
            "turn": asdict(_summarize("turn", all_turn)),
            "per_task_turn_p50_ms": asdict(_summarize("per_task_turn_p50", per_task_turn_p50)),
        },
        "per_task_summaries": [
            {
                "task_id": tr.task_id,
                "rotations": tr.rotations,
                "retries_dropped": tr.retries_dropped,
                "overload_retries": tr.overload_retries,
                "turn": asdict(_summarize("turn", tr.turn_ms)),
            }
            for tr in task_results
        ],
        "raw_ms": {
            "get_items_limit_20": all_get,
            "add_items_2": all_add,
            "turn": all_turn,
        },
    }


# ---------------------------------------------------------------------------
# Environment fingerprinting
# ---------------------------------------------------------------------------


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True
        )
        return bool(out.strip())
    except Exception:
        return None


def _aerospike_server_version(client: Any) -> str | None:
    try:
        info = client.info_all("build")
        # info_all returns { node: (err, response) }
        for _, (_, resp) in info.items():
            if resp:
                stripped: str = resp.strip().removeprefix("build\t")
                return stripped
    except Exception:
        return None
    return None


def _package_version(name: str) -> str | None:
    """Return ``name``'s installed distribution version, or None if unknown."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(name)
        except PackageNotFoundError:
            pass
    except Exception:
        return None
    # Fall back to a ``__version__`` attribute if the dist lookup failed.
    try:
        mod = __import__(name.replace("-", "_"))
        attr = getattr(mod, "__version__", None)
        return str(attr) if attr is not None else None
    except Exception:
        return None


def _capture_environment(client: Any, backend: str) -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "git_sha": _git_sha(),
        "git_dirty": _git_dirty(),
        "python": {
            "version": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or None,
            "cpu_count": os.cpu_count(),
        },
        "aerospike": {
            "client_version": _package_version("aerospike"),
            "server_build": _aerospike_server_version(client),
            "host": os.environ.get("AEROSPIKE_HOST"),
            "namespace": os.environ.get("AEROSPIKE_NAMESPACE", "test"),
        },
        "openai_agents_version": _package_version("openai-agents"),
        "openai_agents_aerospike_version": _package_version("openai-agents-aerospike"),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown_summary(env: dict[str, Any], variants: list[dict[str, Any]]) -> str:
    """Render a human-readable summary suitable for pasting into docs."""
    lines: list[str] = []
    lines.append(f"# Benchmark run {env['timestamp_utc']}")
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- backend: `{env['backend']}`")
    lines.append(f"- git: `{env['git_sha']}` (dirty: {env['git_dirty']})")
    py = env["platform"]
    lines.append(
        f"- host: {py['system']} {py['release']} / {py['machine']} / "
        f"{py['processor']} / {py['cpu_count']} logical CPUs"
    )
    lines.append(f"- python: {env['python']['version']} ({env['python']['implementation']})")
    aero = env["aerospike"]
    lines.append(f"- aerospike client: {aero['client_version']}, server: {aero['server_build']}")
    lines.append(f"- openai-agents: {env['openai_agents_version']}")
    lines.append("")

    lines.append("## Results")
    lines.append("")
    lines.append("| backend | depth | C | op | n | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |")
    lines.append("|---|---:|---:|---|---:|---:|---:|---:|---:|")
    for variant in variants:
        backend = variant["backend"]
        depth = variant["history_depth_before_bench"]
        concurrency = variant["concurrency"]
        for op_key, label in (
            ("get_items_limit_20", "get_items(limit=20)"),
            ("add_items_2", "add_items(2)"),
            ("turn", "turn"),
        ):
            s = variant["summary"][op_key]
            lines.append(
                f"| `{backend}` | {depth} | {concurrency} | {label} | {s['n']} | "
                f"{s['p50_ms']:.3f} | {s['p95_ms']:.3f} | {s['p99_ms']:.3f} | "
                f"{s['mean_ms']:.3f} |"
            )
    lines.append("")

    # Throughput and fairness tables are only meaningful once concurrency > 1.
    if any(v["concurrency"] > 1 for v in variants):
        lines.append("## Throughput")
        lines.append("")
        lines.append("Measured turns (across all tasks) divided by the wall-clock time of the")
        lines.append("parallel gather(). Warmup and rotation retries do not contribute.")
        lines.append("")
        lines.append("| backend | depth | C | throughput (turns/s) | wall (s) |")
        lines.append("|---|---:|---:|---:|---:|")
        for variant in variants:
            lines.append(
                f"| `{variant['backend']}` | "
                f"{variant['history_depth_before_bench']} | "
                f"{variant['concurrency']} | "
                f"{variant['throughput_turns_per_second']:.1f} | "
                f"{variant['wall_clock_seconds']:.3f} |"
            )
        lines.append("")

        lines.append("## Fairness (per-task turn p50 distribution)")
        lines.append("")
        lines.append("A uniformly fast run has a tight per-task p50 distribution; a large gap")
        lines.append("between min and max indicates one or more tasks are starving.")
        lines.append("")
        lines.append(
            "| backend | depth | C | tasks | min p50 (ms) | p50 p50 (ms) | "
            "max p50 (ms) | stdev (ms) |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for variant in variants:
            if variant["concurrency"] <= 1:
                continue
            s = variant["summary"]["per_task_turn_p50_ms"]
            lines.append(
                f"| `{variant['backend']}` | "
                f"{variant['history_depth_before_bench']} | "
                f"{variant['concurrency']} | "
                f"{s['n']} | "
                f"{s['min_ms']:.3f} | {s['p50_ms']:.3f} | "
                f"{s['max_ms']:.3f} | {s['stdev_ms']:.3f} |"
            )
        lines.append("")

    any_rotations = any(v.get("rotations", 0) for v in variants)
    if any_rotations:
        lines.append("## Rotations")
        lines.append("")
        lines.append(
            "The non-sharded `aerospike` backend rotates (clear + re-preload) "
            "when the session record exceeds 1 MiB. Rotation cost is excluded "
            "from the distributions above."
        )
        lines.append("")
        lines.append("| backend | depth | C | rotations | dropped iters |")
        lines.append("|---|---:|---:|---:|---:|")
        for variant in variants:
            if variant.get("rotations", 0):
                lines.append(
                    f"| `{variant['backend']}` | "
                    f"{variant['history_depth_before_bench']} | "
                    f"{variant['concurrency']} | "
                    f"{variant['rotations']} | "
                    f"{variant.get('retries_dropped', 0)} |"
                )
        lines.append("")

    any_overload = any(v.get("overload_retries", 0) for v in variants)
    if any_overload:
        lines.append("## Device-overload retries")
        lines.append("")
        lines.append(
            "Aerospike nodes raise `DeviceOverload` when their write queue "
            "to persistent storage can't keep up. The harness backs off with "
            "jittered exponential delay and retries the same iteration; the "
            "retried attempts are excluded from the distributions above."
        )
        lines.append("")
        lines.append("| backend | depth | C | overload retries |")
        lines.append("|---|---:|---:|---:|")
        for variant in variants:
            if variant.get("overload_retries", 0):
                lines.append(
                    f"| `{variant['backend']}` | "
                    f"{variant['history_depth_before_bench']} | "
                    f"{variant['concurrency']} | "
                    f"{variant['overload_retries']} |"
                )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    factory = _BACKEND_FACTORIES[args.backend]

    host = os.environ.get("AEROSPIKE_HOST") or args.aerospike_host
    if not host:
        raise SystemExit(
            "Set AEROSPIKE_HOST or pass --aerospike-host to point at a running cluster."
        )
    port = int(os.environ.get("AEROSPIKE_PORT", args.aerospike_port))

    client = aerospike.client({"hosts": [(host, port)]}).connect()

    env = _capture_environment(client, args.backend)

    depths = [int(d) for d in args.history_depth.split(",") if d.strip()]
    concurrencies = [int(c) for c in args.concurrency.split(",") if c.strip()]
    if any(c < 1 for c in concurrencies):
        raise SystemExit("--concurrency values must be >= 1")
    variants: list[dict[str, Any]] = []

    try:
        for depth in depths:
            for concurrency in concurrencies:
                print(
                    f"=> running backend={args.backend} depth={depth} "
                    f"concurrency={concurrency} "
                    f"warmup={args.warmup} iters={args.iterations}",
                    flush=True,
                )
                variant = await _run_one_variant(
                    backend=args.backend,
                    factory=factory,
                    client=client,
                    depth=depth,
                    concurrency=concurrency,
                    user_size=args.user_size,
                    assistant_size=args.assistant_size,
                    warmup=args.warmup,
                    iterations=args.iterations,
                    ttl=args.ttl,
                )
                variants.append(variant)
                _print_variant_summary(variant)
                if args.cool_down_seconds > 0:
                    await asyncio.sleep(args.cool_down_seconds)
    finally:
        client.close()

    output_path = _resolve_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "environment": env,
        "config": {
            "backend": args.backend,
            "history_depths": depths,
            "concurrencies": concurrencies,
            "user_size_bytes": args.user_size,
            "assistant_size_bytes": args.assistant_size,
            "warmup": args.warmup,
            "iterations": args.iterations,
            "ttl": args.ttl,
        },
        "variants": variants,
    }
    with output_path.open("w") as fh:
        json.dump(payload, fh, indent=2)

    markdown_path = output_path.with_suffix(".md")
    with markdown_path.open("w") as fh:
        fh.write(_render_markdown_summary(env, variants))

    print()
    print(f"Raw results: {output_path}")
    print(f"Summary:     {markdown_path}")


def _resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output:
        return Path(args.output)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_dir = Path(__file__).resolve().parent / "results"
    return default_dir / f"{ts}-{args.backend}.json"


def _print_variant_summary(variant: dict[str, Any]) -> None:
    summary = variant["summary"]
    depth = variant["history_depth_before_bench"]
    concurrency = variant["concurrency"]
    for op_key, label in (
        ("get_items_limit_20", "get_items(limit=20)"),
        ("add_items_2", "add_items(2 items) "),
        ("turn", "turn                "),
    ):
        s = summary[op_key]
        print(
            f"   depth={depth:<5} C={concurrency:<4} {label} "
            f"n={s['n']:<6} p50={s['p50_ms']:7.3f}ms "
            f"p95={s['p95_ms']:7.3f}ms p99={s['p99_ms']:7.3f}ms "
            f"mean={s['mean_ms']:7.3f}ms"
        )
    if concurrency > 1:
        print(
            f"   depth={depth:<5} C={concurrency:<4} "
            f"throughput={variant['throughput_turns_per_second']:8.1f} turns/s "
            f"(wall={variant['wall_clock_seconds']:.3f}s)"
        )
        s = summary["per_task_turn_p50_ms"]
        print(
            f"   depth={depth:<5} C={concurrency:<4} per-task turn-p50 "
            f"min={s['min_ms']:6.3f}ms median={s['p50_ms']:6.3f}ms "
            f"max={s['max_ms']:6.3f}ms stdev={s['stdev_ms']:6.3f}ms"
        )
    rotations = variant.get("rotations", 0)
    if rotations:
        print(
            f"   depth={depth:<5} C={concurrency:<4} "
            f"rotations={rotations} dropped_iters={variant.get('retries_dropped', 0)}"
        )
    overload_retries = variant.get("overload_retries", 0)
    if overload_retries:
        print(f"   depth={depth:<5} C={concurrency:<4} overload_retries={overload_retries}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--backend",
        choices=sorted(_BACKEND_FACTORIES.keys()),
        default="aerospike",
        help="Session backend to exercise.",
    )
    parser.add_argument(
        "--history-depth",
        default="0,50,200",
        help=(
            "Comma-separated list of pre-loaded session depths to measure at. "
            "Depths where (depth + 2 * iterations) * item_size exceeds 1 MiB "
            "are not feasible for the non-sharded 'aerospike' backend; the "
            "harness will rotate sessions mid-run and report the count."
        ),
    )
    parser.add_argument(
        "--concurrency",
        default="1",
        help=(
            "Comma-separated list of concurrency levels. For each value C, C "
            "parallel asyncio tasks each drive their own session through the "
            "same Aerospike client. Default: 1 (single session)."
        ),
    )
    parser.add_argument(
        "--user-size",
        type=int,
        default=512,
        help="Size in bytes of each user message's content field.",
    )
    parser.add_argument(
        "--assistant-size",
        type=int,
        default=1024,
        help="Size in bytes of each assistant message's content field.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=500,
        help="Measured iterations per variant (post-warmup).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=50,
        help="Warmup iterations per variant (discarded).",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help=(
            "Session TTL in seconds. Default: use the namespace default. "
            "Aerospike CE's out-of-the-box 'test' namespace refuses non-zero "
            "TTLs (allow-ttl-without-nsup=false, nsup-period=0); point the "
            "harness at a namespace configured for TTLs before setting this."
        ),
    )
    parser.add_argument(
        "--cool-down-seconds",
        type=float,
        default=0.0,
        help=(
            "Sleep for this many seconds between variants. Useful on "
            "low-spec local clusters where the server's background "
            "defragger needs to catch up after a concurrent write burst "
            "before the next variant begins."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Explicit output JSON path. Default: benchmarks/results/<ts>-<backend>.json",
    )
    parser.add_argument(
        "--aerospike-host",
        default="127.0.0.1",
        help="Seed host (fallback if AEROSPIKE_HOST is unset).",
    )
    parser.add_argument(
        "--aerospike-port",
        type=int,
        default=3000,
        help="Seed port (fallback if AEROSPIKE_PORT is unset).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
