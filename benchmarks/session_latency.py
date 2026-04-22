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
import concurrent.futures
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
from urllib.parse import urlparse, urlunparse

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


def _sanitize_url(url: str) -> str:
    """Return ``url`` with any embedded password stripped, for logging."""
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if parsed.password is None:
        return url
    netloc = parsed.hostname or ""
    if parsed.username:
        netloc = f"{parsed.username}:***@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


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
# Backend abstraction
# ---------------------------------------------------------------------------


class _Backend:
    """Abstract benchmark backend.

    A backend owns the client / engine / file handle shared across all
    sessions in a variant, constructs per-session adapter instances, and
    declares which exception types the harness should treat as
    transient-retryable (``retryable_overload_exceptions``) or as the
    backend-specific equivalent of Aerospike's single-record overflow
    (``record_too_large_exceptions``).
    """

    name: str = ""

    async def setup(self) -> None:
        """Open the shared client / engine once per variant run."""

    async def teardown(self) -> None:
        """Release the shared client / engine at the end of the run."""

    def build_session(self, session_id: str, ttl: int | None) -> Any:
        raise NotImplementedError

    def retryable_overload_exceptions(self) -> tuple[type[BaseException], ...]:
        """Exceptions the harness should catch, sleep, and retry."""
        return ()

    def record_too_large_exceptions(self) -> tuple[type[BaseException], ...]:
        """Exceptions signaling the session outgrew the backend's record cap."""
        return ()

    def environment_extras(self) -> dict[str, Any]:
        """Backend-specific data to embed in the environment fingerprint."""
        return {}


class _AerospikeBackend(_Backend):
    name = "aerospike"
    _session_cls: type[AerospikeSession] = AerospikeSession

    def __init__(self, *, host: str, port: int, namespace: str, set_name: str) -> None:
        self.host = host
        self.port = port
        self.namespace = namespace
        self.set_name = set_name
        self._client: Any = None

    @property
    def client(self) -> Any:
        return self._client

    async def setup(self) -> None:
        # The default max_error_rate=100 / error_rate_window=1 defensive
        # circuit-breaker in the Aerospike C client is designed for real
        # apps where a runaway error spike means "the cluster is sick, stop
        # hammering it." For benchmarks that's harmful: the harness already
        # has its own overload-backoff wrapper (see _call_with_overload_backoff),
        # so transient DeviceOverload errors get retried, and the circuit-
        # breaker counts each retry against the per-second error budget.
        # At C>=16 we trip it within a second and the whole benchmark aborts.
        #
        # We can't simply set max_error_rate=0: the Python client silently
        # reverts both knobs to their defaults whenever the ratio
        # max_error_rate/error_rate_window is outside [1, 100], and 0/anything
        # rounds to 0 which is < 1 (see the client docs at "Client
        # Configuration > max_error_rate"). So the way to raise the threshold
        # as far as the SDK will let us is to push the ratio to its maximum
        # allowed value of 100 and widen the window: max_error_rate=10000,
        # error_rate_window=100 tolerates ~100 err/s sustained for 100 tend
        # iterations (~100s) before tripping — well above anything the harness
        # will produce even under heavy retry bursts. This is explicit
        # benchmark harness behaviour, not a library default. Documented here
        # rather than hidden behind an env var so it's obvious in the results
        # fingerprint.
        config: dict[str, Any] = {
            "hosts": [(self.host, self.port)],
            "max_error_rate": 10000,
            "error_rate_window": 100,
        }
        self._client = aerospike.client(config).connect()

    async def teardown(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def build_session(self, session_id: str, ttl: int | None) -> Any:
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "client": self._client,
            "namespace": self.namespace,
            "set_name": self.set_name,
        }
        if ttl is not None:
            kwargs["ttl"] = ttl
        return self._session_cls(**kwargs)

    def retryable_overload_exceptions(self) -> tuple[type[BaseException], ...]:
        if _DEVICE_OVERLOAD is not None:
            return (_DEVICE_OVERLOAD,)
        return ()

    def record_too_large_exceptions(self) -> tuple[type[BaseException], ...]:
        return (SessionRecordTooLargeError,)

    def environment_extras(self) -> dict[str, Any]:
        return {
            "aerospike": {
                "client_version": _package_version("aerospike"),
                "server_build": _aerospike_server_version(self._client),
                "host": self.host,
                "port": self.port,
                "namespace": self.namespace,
                "set_name": self.set_name,
                "max_error_rate": 10000,
                "error_rate_window": 100,
                "max_error_rate_note": (
                    "raised to the SDK's maximum allowed ratio (100) over a "
                    "wider window so the harness's overload-retry bursts "
                    "don't trip the circuit breaker; the SDK silently "
                    "reverts ratios outside [1, 100] to defaults"
                ),
            }
        }


class _AerospikeShardedBackend(_AerospikeBackend):
    name = "aerospike-sharded"
    _session_cls = ShardedAerospikeSession


class _RedisBackend(_Backend):
    """``openai-agents`` upstream ``RedisSession`` against a shared client."""

    name = "redis"

    def __init__(self, *, url: str, key_prefix: str) -> None:
        self.url = url
        self.key_prefix = key_prefix
        self._client: Any = None

    async def setup(self) -> None:
        import redis.asyncio as redis

        # Single shared async Redis client for every task in the variant;
        # fail fast if the URL is wrong so a bad config doesn't just
        # surface mid-benchmark as a timeout.
        self._client = redis.from_url(self.url)
        await self._client.ping()

    async def teardown(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except AttributeError:  # redis<5 used close()
                await self._client.close()
            self._client = None

    def build_session(self, session_id: str, ttl: int | None) -> Any:
        from agents.extensions.memory.redis_session import RedisSession

        return RedisSession(
            session_id=session_id,
            redis_client=self._client,
            key_prefix=self.key_prefix,
            ttl=ttl,
        )

    def environment_extras(self) -> dict[str, Any]:
        return {
            "redis": {
                "client_version": _package_version("redis"),
                "url": _sanitize_url(self.url),
                "key_prefix": self.key_prefix,
            }
        }


class _SQLAlchemyBackend(_Backend):
    """``openai-agents`` upstream ``SQLAlchemySession`` against a shared engine."""

    name = "sqlalchemy"

    def __init__(
        self,
        *,
        url: str,
        sessions_table: str,
        messages_table: str,
        pool_size: int,
    ) -> None:
        self.url = url
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self.pool_size = pool_size
        self._engine: Any = None

    async def setup(self) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        self._engine = create_async_engine(
            self.url,
            pool_size=self.pool_size,
            max_overflow=self.pool_size,
            pool_pre_ping=True,
        )

        # Ensure the schema exists before any task tries to read/write.
        # SQLAlchemySession lazily creates its tables; doing it here
        # serializes the DDL so concurrent tasks don't race on CREATE.
        from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

        # SQLAlchemySession intentionally has no close() — the engine is
        # owned at variant scope and disposed in teardown(), so we don't
        # release anything per-bootstrap-session.
        bootstrap = SQLAlchemySession(
            session_id=f"bench-bootstrap-{uuid.uuid4().hex[:8]}",
            engine=self._engine,
            create_tables=True,
            sessions_table=self.sessions_table,
            messages_table=self.messages_table,
        )
        await bootstrap.get_items()

    async def teardown(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def build_session(self, session_id: str, ttl: int | None) -> Any:
        from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

        # SQLAlchemySession has no TTL concept, so ``ttl`` is ignored here.
        return SQLAlchemySession(
            session_id=session_id,
            engine=self._engine,
            create_tables=False,
            sessions_table=self.sessions_table,
            messages_table=self.messages_table,
        )

    def environment_extras(self) -> dict[str, Any]:
        return {
            "sqlalchemy": {
                "version": _package_version("sqlalchemy"),
                "url": _sanitize_url(self.url),
                "sessions_table": self.sessions_table,
                "messages_table": self.messages_table,
                "pool_size": self.pool_size,
            }
        }


class _SQLiteBackend(_Backend):
    """``openai-agents`` upstream ``SQLiteSession`` (baseline / single-process)."""

    name = "sqlite"

    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path

    def build_session(self, session_id: str, ttl: int | None) -> Any:
        from agents.memory.sqlite_session import SQLiteSession

        return SQLiteSession(session_id=session_id, db_path=self.db_path)

    def environment_extras(self) -> dict[str, Any]:
        return {"sqlite": {"db_path": self.db_path}}


_BACKEND_NAMES = (
    "aerospike",
    "aerospike-sharded",
    "redis",
    "sqlalchemy",
    "sqlite",
)


def _build_backend(args: argparse.Namespace) -> _Backend:
    """Construct the backend selected by ``args.backend``."""
    name = args.backend
    if name in ("aerospike", "aerospike-sharded"):
        host = os.environ.get("AEROSPIKE_HOST") or args.aerospike_host
        if not host:
            raise SystemExit(
                "Set AEROSPIKE_HOST or pass --aerospike-host to point at a running cluster."
            )
        port = int(os.environ.get("AEROSPIKE_PORT", args.aerospike_port))
        namespace = os.environ.get("AEROSPIKE_NAMESPACE", "test")
        cls: type[_AerospikeBackend] = (
            _AerospikeShardedBackend if name == "aerospike-sharded" else _AerospikeBackend
        )
        return cls(host=host, port=port, namespace=namespace, set_name="benchmark")
    if name == "redis":
        url = os.environ.get("REDIS_URL") or args.redis_url
        if not url:
            raise SystemExit("Set REDIS_URL or pass --redis-url for the redis backend.")
        return _RedisBackend(url=url, key_prefix="bench:session")
    if name == "sqlalchemy":
        url = os.environ.get("SQLALCHEMY_URL") or args.sqlalchemy_url
        if not url:
            raise SystemExit(
                "Set SQLALCHEMY_URL or pass --sqlalchemy-url for the sqlalchemy backend "
                "(example: postgresql+asyncpg://user:pw@host/db)."
            )
        return _SQLAlchemyBackend(
            url=url,
            sessions_table="bench_sessions",
            messages_table="bench_messages",
            pool_size=args.sqlalchemy_pool_size,
        )
    if name == "sqlite":
        db_path = args.sqlite_path
        return _SQLiteBackend(db_path=db_path)
    raise SystemExit(f"Unknown backend: {name}")


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


async def _call_with_overload_backoff(
    coro_factory: Callable[[], Any],
    retryable: tuple[type[BaseException], ...],
) -> Any:
    """Await ``coro_factory()`` with jittered retry on backend-declared overload.

    ``coro_factory`` is called fresh each retry — a coroutine object can
    only be awaited once. ``retryable`` is the backend's declared set of
    transient-retryable exception types; passing ``()`` makes this a
    thin pass-through.
    """
    attempts = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:
            if retryable and isinstance(exc, retryable):
                attempts += 1
                if attempts > _MAX_OVERLOAD_RETRIES_PER_ITER:
                    raise
                backoff = min(0.25, 0.005 * (2 ** min(attempts, 6)))
                await asyncio.sleep(backoff * (0.5 + random.random()))
                continue
            raise


async def _preload_session(
    session: Any,
    *,
    depth: int,
    user_size: int,
    assistant_size: int,
    retryable: tuple[type[BaseException], ...],
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

        await _call_with_overload_backoff(_add, retryable)


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
    session_slot: int,
    session_salt: str,
    slot_primary: bool,
    setup_event: asyncio.Event | None,
    backend: _Backend,
    depth: int,
    user_size: int,
    assistant_size: int,
    warmup: int,
    iterations: int,
    ttl: int | None,
) -> _TaskResult:
    """Drive a single session through warmup + measurement.

    A *task* is one asyncio task exercising one ``session_slot`` (logical
    session) against the shared backend client. Tasks and sessions are
    separate concepts: when ``--sessions`` < ``--concurrency``, multiple
    tasks share a session_slot (modelling contention on a hot record),
    and per-task cleanup/warmup is gated so we don't step on each other.
    """
    retryable = backend.retryable_overload_exceptions()
    too_large = backend.record_too_large_exceptions()

    # session_slot identifies the shared record; session_salt makes it
    # unique per variant so we don't collide with leftover state from
    # an earlier sweep. task_id is deliberately not in the session_id:
    # two tasks with the same session_slot MUST produce the same
    # session_id or the shared-record experiment is pointless.
    session_id = f"bench-{backend.name}-{depth}-s{session_slot}-{session_salt}"
    session = backend.build_session(session_id, ttl)

    # Only the slot primary clears and preloads; followers wait for the
    # shared record to be ready before measurement starts. When sessions
    # == concurrency (the default), every task is its own primary and
    # this is a no-op event set immediately.
    if setup_event is None or slot_primary:
        await _call_with_overload_backoff(session.clear_session, retryable)
        await _preload_session(
            session,
            depth=depth,
            user_size=user_size,
            assistant_size=assistant_size,
            retryable=retryable,
        )
        if setup_event is not None:
            setup_event.set()
    else:
        await setup_event.wait()

    get_ms: list[float] = []
    add_ms: list[float] = []
    turn_ms: list[float] = []
    rotations = 0
    retries_dropped = 0
    overload_retries = 0

    async def _rotate() -> None:
        nonlocal rotations
        rotations += 1
        await _call_with_overload_backoff(session.clear_session, retryable)
        await _preload_session(
            session,
            depth=depth,
            user_size=user_size,
            assistant_size=assistant_size,
            retryable=retryable,
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
            except Exception as exc:
                if too_large and isinstance(exc, too_large):
                    # Backend hit its per-record cap. Reset and retry the
                    # same iteration slot so we still collect
                    # ``iterations`` measured turns. Only Aerospike
                    # declares this today.
                    retries_dropped += 1
                    await _rotate()
                    iter_overload_retries = 0
                    continue
                if retryable and isinstance(exc, retryable):
                    # Transient backend throttling. Back off and retry
                    # the same iteration. The failed op's timing is
                    # discarded; only cleanly completed turns feed the
                    # distribution.
                    overload_retries += 1
                    iter_overload_retries += 1
                    if iter_overload_retries > _MAX_OVERLOAD_RETRIES_PER_ITER:
                        raise
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
        # When sessions < concurrency, only the slot primary clears the
        # record so followers don't wipe it out from under a sibling
        # that's still finishing its last iteration.
        if slot_primary:
            try:
                await session.clear_session()
            except Exception:
                pass
        try:
            await session.close()
        except Exception:
            pass

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
    backend: _Backend,
    depth: int,
    concurrency: int,
    sessions: int | None,
    user_size: int,
    assistant_size: int,
    warmup: int,
    iterations: int,
    ttl: int | None,
) -> dict[str, Any]:
    """Run one (backend, depth, concurrency) variant and return summaries.

    ``concurrency`` tasks share a single backend-owned client (that's the
    connection pool we're trying to exercise — one client per task would
    hide whatever the pool does under load). Each task is assigned a
    ``session_slot`` via ``task_id % num_sessions`` where ``num_sessions``
    defaults to ``concurrency`` (realistic agent shape: one session per
    task) and can be narrowed via ``--sessions`` to deliberately create
    contention on a smaller set of records.
    """
    num_sessions = concurrency if sessions is None else min(sessions, concurrency)
    # Fresh salt per variant so retries from an earlier failed variant
    # don't leak state into this one.
    session_salt = uuid.uuid4().hex[:8]
    # One coordination event per slot, set by the slot primary after
    # clear + preload so followers know the record is ready. None when
    # sessions == concurrency — every task is its own primary, no
    # barrier needed.
    setup_events: list[asyncio.Event | None]
    if num_sessions < concurrency:
        setup_events = [asyncio.Event() for _ in range(num_sessions)]
    else:
        setup_events = [None] * num_sessions

    # A slot's primary is the lowest task_id that maps to it. With the
    # modulo assignment below, that's always the task whose id < num_sessions.
    tasks = [
        _run_one_task(
            task_id=t,
            session_slot=t % num_sessions,
            session_salt=session_salt,
            slot_primary=(t < num_sessions),
            setup_event=setup_events[t % num_sessions],
            backend=backend,
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
        "backend": backend.name,
        "history_depth_before_bench": depth,
        "concurrency": concurrency,
        "sessions": num_sessions,
        "tasks_per_session": concurrency / num_sessions if num_sessions else 0,
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


def _capture_environment(backend: _Backend) -> dict[str, Any]:
    env: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "backend": backend.name,
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
        "openai_agents_version": _package_version("openai-agents"),
        "openai_agents_aerospike_version": _package_version("openai-agents-aerospike"),
    }
    env.update(backend.environment_extras())
    return env


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
    if "aerospike" in env:
        aero = env["aerospike"]
        lines.append(
            f"- aerospike client: {aero.get('client_version')}, server: {aero.get('server_build')}"
        )
    if "redis" in env:
        r = env["redis"]
        lines.append(f"- redis client: {r.get('client_version')} @ {r.get('url')}")
    if "sqlalchemy" in env:
        sa = env["sqlalchemy"]
        lines.append(f"- sqlalchemy: {sa.get('version')} @ {sa.get('url')}")
    if "sqlite" in env:
        lines.append(f"- sqlite db_path: {env['sqlite'].get('db_path')}")
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
    depths = [int(d) for d in args.history_depth.split(",") if d.strip()]
    concurrencies = [int(c) for c in args.concurrency.split(",") if c.strip()]
    if any(c < 1 for c in concurrencies):
        raise SystemExit("--concurrency values must be >= 1")

    # Resize asyncio's default ThreadPoolExecutor before any to_thread calls.
    # The sync Aerospike client dispatches via to_thread, so if the pool is
    # smaller than concurrency, every variant's effective parallelism is
    # capped at the pool size rather than the --concurrency flag. Python's
    # default is min(32, cpu+4), which on an 8-vCPU client silently caps us
    # at ~12 and masquerades as server-side saturation.
    max_concurrency = max(concurrencies)
    if args.max_worker_threads is not None:
        pool_size = max(1, args.max_worker_threads)
    else:
        pool_size = max(64, 4 * max_concurrency)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix="bench-worker",
        )
    )
    print(f"=> asyncio default-executor max_workers={pool_size}", flush=True)

    if args.sessions is not None:
        if args.sessions < 1:
            raise SystemExit("--sessions must be >= 1")
        if args.sessions > max_concurrency:
            print(
                f"=> warning: --sessions={args.sessions} > max --concurrency "
                f"({max_concurrency}); some sessions will never be touched",
                flush=True,
            )

    backend = _build_backend(args)
    await backend.setup()

    env = _capture_environment(backend)
    env["harness"] = {
        "executor_max_workers": pool_size,
        "sessions_override": args.sessions,
    }
    variants: list[dict[str, Any]] = []

    try:
        for depth in depths:
            for concurrency in concurrencies:
                print(
                    f"=> running backend={backend.name} depth={depth} "
                    f"concurrency={concurrency} "
                    f"warmup={args.warmup} iters={args.iterations}",
                    flush=True,
                )
                variant = await _run_one_variant(
                    backend=backend,
                    depth=depth,
                    concurrency=concurrency,
                    sessions=args.sessions,
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
        await backend.teardown()

    output_path = _resolve_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "environment": env,
        "config": {
            "backend": args.backend,
            "history_depths": depths,
            "concurrencies": concurrencies,
            "sessions": args.sessions,
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
        choices=_BACKEND_NAMES,
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
        "--sessions",
        type=int,
        default=None,
        help=(
            "Number of distinct session_ids to fan tasks across. Default "
            "(None) gives every task its own session (the realistic agent "
            "concurrency shape). Set to 1 to force every task onto one "
            "record (pathological hot-key stress test). Set to a value "
            "between 1 and --concurrency to study partial contention."
        ),
    )
    parser.add_argument(
        "--max-worker-threads",
        type=int,
        default=None,
        help=(
            "Size of asyncio's default ThreadPoolExecutor, which the "
            "sync Aerospike client dispatches into via asyncio.to_thread. "
            "Python's default is min(32, cpu+4) = ~12 on an 8-vCPU box, "
            "which silently caps concurrent C-client calls regardless of "
            "--concurrency. Default here: max(64, 4 * max concurrency). "
            "Raise this if you see throughput plateau but server CPU idle."
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
    parser.add_argument(
        "--redis-url",
        default="redis://127.0.0.1:6379/0",
        help="Redis connection URL (fallback if REDIS_URL is unset).",
    )
    parser.add_argument(
        "--sqlalchemy-url",
        default=None,
        help=(
            "SQLAlchemy async URL for the sqlalchemy backend "
            "(example: postgresql+asyncpg://user:pw@host:5432/dbname). "
            "Falls back to SQLALCHEMY_URL."
        ),
    )
    parser.add_argument(
        "--sqlalchemy-pool-size",
        type=int,
        default=32,
        help=(
            "Connection pool size for the sqlalchemy backend. Should be "
            ">= your highest --concurrency value or workers will queue "
            "on the pool instead of hitting the database."
        ),
    )
    parser.add_argument(
        "--sqlite-path",
        default=":memory:",
        help=(
            "Path to the SQLite file for the sqlite backend. Default "
            "':memory:' is per-connection in-memory and only meaningful "
            "as a best-case baseline; pass a real file to measure "
            "on-disk SQLite."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
