"""Session latency benchmark harness.

Measures end-to-end agent-turn latency for the session backends in this
repository. A *turn* is the unit of work the SDK's ``Runner`` performs per
user input::

    get_items(limit=20)             # load the recent conversation
    add_items([user_msg, assistant_msg])   # persist the new exchange

Both per-op and full-turn timings are captured at p50 / p95 / p99 / mean,
across a configurable grid of history depths (how many items were already
in the session) and item sizes. Raw timings, the summary, and an
environment fingerprint are written as a single JSON file so downstream
analysis tools can plot distributions or compare runs.

Run::

    # Start an Aerospike CE server locally
    docker run -d --name aerospike -p 3000-3002:3000-3002 \\
        aerospike/aerospike-server:latest

    AEROSPIKE_HOST=127.0.0.1 python benchmarks/session_latency.py \\
        --backend aerospike \\
        --history-depth 0,50,200,1000 \\
        --iterations 500 --warmup 50

    AEROSPIKE_HOST=127.0.0.1 python benchmarks/session_latency.py \\
        --backend aerospike-sharded \\
        --history-depth 0,50,200,1000 \\
        --iterations 500 --warmup 50

Output lands in ``benchmarks/results/`` by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
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
        await session.add_items(items)  # type: ignore[arg-type]


async def _run_one_variant(
    *,
    backend: str,
    factory: Callable[..., AerospikeSession],
    client: Any,
    depth: int,
    user_size: int,
    assistant_size: int,
    warmup: int,
    iterations: int,
    ttl: int | None,
) -> dict[str, Any]:
    """Run one (backend, depth) variant and return raw + summary data."""
    session_id = f"bench-{backend}-{depth}-{uuid.uuid4().hex[:8]}"
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
    await session.clear_session()
    await _preload_session(session, depth=depth, user_size=user_size, assistant_size=assistant_size)

    # Timings in milliseconds.
    get_ms: list[float] = []
    add_ms: list[float] = []
    turn_ms: list[float] = []

    rotations = 0
    retries_dropped = 0

    async def _rotate() -> None:
        """Reset the session back to its preloaded state.

        Used when ``AerospikeSession`` (not sharded) overflows the 1 MiB
        record limit mid-run. The rotation cost is deliberately excluded
        from measurement: we just discard the offending iteration's
        timings and continue.
        """
        nonlocal rotations
        rotations += 1
        await session.clear_session()
        await _preload_session(
            session, depth=depth, user_size=user_size, assistant_size=assistant_size
        )

    try:
        total_iters = warmup + iterations
        i = 0
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
                continue

            if i >= warmup:
                get_ms.append((t1 - t0) * 1000.0)
                add_ms.append((t2 - t1) * 1000.0)
                turn_ms.append((t2 - t0) * 1000.0)
            i += 1
    finally:
        await session.clear_session()
        await session.close()

    return {
        "backend": backend,
        "history_depth_before_bench": depth,
        "warmup": warmup,
        "iterations": iterations,
        "user_size_bytes": user_size,
        "assistant_size_bytes": assistant_size,
        "rotations": rotations,
        "retries_dropped": retries_dropped,
        "summary": {
            "get_items_limit_20": asdict(_summarize("get_items(limit=20)", get_ms)),
            "add_items_2": asdict(_summarize("add_items(2)", add_ms)),
            "turn": asdict(_summarize("turn", turn_ms)),
        },
        "raw_ms": {
            "get_items_limit_20": get_ms,
            "add_items_2": add_ms,
            "turn": turn_ms,
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
    lines.append(
        "| backend | history depth | op | n | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) |"
    )
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|")
    for variant in variants:
        backend = variant["backend"]
        depth = variant["history_depth_before_bench"]
        for op_key, label in (
            ("get_items_limit_20", "get_items(limit=20)"),
            ("add_items_2", "add_items(2)"),
            ("turn", "turn"),
        ):
            s = variant["summary"][op_key]
            lines.append(
                f"| `{backend}` | {depth} | {label} | {s['n']} | "
                f"{s['p50_ms']:.3f} | {s['p95_ms']:.3f} | {s['p99_ms']:.3f} | "
                f"{s['mean_ms']:.3f} |"
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
        lines.append("| backend | history depth | rotations | dropped iters |")
        lines.append("|---|---:|---:|---:|")
        for variant in variants:
            if variant.get("rotations", 0):
                lines.append(
                    f"| `{variant['backend']}` | "
                    f"{variant['history_depth_before_bench']} | "
                    f"{variant['rotations']} | "
                    f"{variant.get('retries_dropped', 0)} |"
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
    variants: list[dict[str, Any]] = []

    try:
        for depth in depths:
            print(
                f"=> running backend={args.backend} depth={depth} "
                f"warmup={args.warmup} iters={args.iterations}",
                flush=True,
            )
            variant = await _run_one_variant(
                backend=args.backend,
                factory=factory,
                client=client,
                depth=depth,
                user_size=args.user_size,
                assistant_size=args.assistant_size,
                warmup=args.warmup,
                iterations=args.iterations,
                ttl=args.ttl,
            )
            variants.append(variant)
            _print_variant_summary(variant)
    finally:
        client.close()

    output_path = _resolve_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "environment": env,
        "config": {
            "backend": args.backend,
            "history_depths": depths,
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
    for op_key, label in (
        ("get_items_limit_20", "get_items(limit=20)"),
        ("add_items_2", "add_items(2 items) "),
        ("turn", "turn                "),
    ):
        s = summary[op_key]
        print(
            f"   depth={depth:<5} {label} "
            f"n={s['n']:<5} p50={s['p50_ms']:7.3f}ms "
            f"p95={s['p95_ms']:7.3f}ms p99={s['p99_ms']:7.3f}ms "
            f"mean={s['mean_ms']:7.3f}ms"
        )
    rotations = variant.get("rotations", 0)
    if rotations:
        print(
            f"   depth={depth:<5} rotations={rotations} "
            f"dropped_iters={variant.get('retries_dropped', 0)}"
        )


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
