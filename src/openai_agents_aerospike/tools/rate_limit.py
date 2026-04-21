"""Rate-limit tool: atomic per-bucket counters with TTL-based windows.

The counter lives in a single Aerospike bin and is incremented via
``operations.increment``, which is atomic on the server side. The TTL on
the record acts as the sliding/tumbling window: when it expires, the
counter effectively resets.
"""

from __future__ import annotations

from typing import Any

from aerospike_helpers.operations import operations as ops
from agents import function_tool

from ._shared import ToolConfig, require, to_thread

_BIN_COUNT = "count"

_config: ToolConfig | None = None


def configure_rate_limit(
    *,
    client: Any,
    namespace: str = "test",
    set_name: str = "rate_limits",
    default_ttl: int | None = 60,
) -> None:
    """Register the Aerospike client and target set for the rate-limit tool.

    Default TTL is 60 seconds (a one-minute tumbling window). Callers can
    override on each tool invocation via the ``window_seconds`` argument.
    """
    global _config
    _config = ToolConfig(
        client=client,
        namespace=namespace,
        set_name=set_name,
        default_ttl=default_ttl,
    )


@function_tool
async def check_rate_limit(bucket_key: str, limit: int, window_seconds: int = 60) -> str:
    """Atomically increment a counter and report whether the limit is exceeded.

    Args:
        bucket_key: Identifier for the rate-limit bucket (for example,
            ``f"user:{user_id}"`` or ``f"tool:{tool_name}:{user_id}"``).
        limit: Maximum number of allowed increments per window.
        window_seconds: TTL in seconds applied to the record. On expiry the
            counter effectively resets to zero.

    Returns:
        ``"allowed:<n>"`` if the current count is at or below ``limit``
        (``n`` is the new count), or ``"denied:<n>"`` if it is exceeded.
    """
    cfg = require(_config, "rate_limit")
    key = (cfg.namespace, cfg.set_name, bucket_key)

    def _op() -> int:
        _, _, bins = cfg.client.operate(
            key,
            [ops.increment(_BIN_COUNT, 1), ops.read(_BIN_COUNT)],
            meta={"ttl": window_seconds},
        )
        return int(bins.get(_BIN_COUNT) or 0)

    count = await to_thread(_op)
    return f"{'allowed' if count <= limit else 'denied'}:{count}"
