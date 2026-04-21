"""Idempotency tool: deduplicate tool-call effects across agent retries.

Usage pattern: before executing a side-effecting tool, the agent calls
``check_idempotency(key)``; if it returns a cached result, the agent uses
that result instead of re-executing. After successful execution, the agent
calls ``record_idempotency(key, result_json, ttl)`` to register the result.

Records are stored in a dedicated set with a configurable default TTL so
stale idempotency keys clean themselves up automatically.
"""

from __future__ import annotations

from typing import Any

from agents import function_tool

from ._shared import ToolConfig, require, to_thread

_BIN_RESULT = "result"

_config: ToolConfig | None = None


def configure_idempotency(
    *,
    client: Any,
    namespace: str = "test",
    set_name: str = "idempotency",
    default_ttl: int | None = 86400,
) -> None:
    """Register the Aerospike client and target set for the idempotency tool.

    Default TTL is 24 hours. Pass ``default_ttl=None`` to use the namespace
    default, or ``default_ttl=-1`` to make idempotency records never expire.
    """
    global _config
    _config = ToolConfig(
        client=client,
        namespace=namespace,
        set_name=set_name,
        default_ttl=default_ttl,
    )


@function_tool
async def check_idempotency(key: str) -> str:
    """Return the cached result for ``key`` if one exists.

    Args:
        key: Application-chosen idempotency key (for example, a hash of the
            tool's arguments).

    Returns:
        The previously recorded result string, or the empty string if no
        record exists yet.
    """
    cfg = require(_config, "idempotency")
    record_key = (cfg.namespace, cfg.set_name, key)

    def _read() -> str:
        try:
            _, _, bins = cfg.client.get(record_key)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) == 2:
                return ""
            raise
        value = bins.get(_BIN_RESULT)
        return value if isinstance(value, str) else ""

    result = await to_thread(_read)
    return str(result)


@function_tool
async def record_idempotency(key: str, result_json: str, ttl: int | None = None) -> str:
    """Record the outcome of a side-effecting operation under ``key``.

    Args:
        key: Application-chosen idempotency key.
        result_json: The result payload to cache (typically JSON).
        ttl: Optional TTL in seconds. ``None`` uses the configured default.

    Returns:
        ``"ok"`` on success.
    """
    cfg = require(_config, "idempotency")
    record_key = (cfg.namespace, cfg.set_name, key)
    effective_ttl = ttl if ttl is not None else cfg.default_ttl
    meta = {"ttl": effective_ttl} if effective_ttl is not None else {}

    def _write() -> None:
        cfg.client.put(record_key, {_BIN_RESULT: result_json}, meta=meta)

    await to_thread(_write)
    return "ok"
