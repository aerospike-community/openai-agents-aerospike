"""Handoff state tool: persist structured state for agent-to-agent handoffs.

A *handoff* is an agent transferring control to another agent, potentially
across processes or workers. This tool stores the handoff payload as a JSON
blob, with optional generation-check ("compare-and-set") writes so two agents
racing to update the same handoff record cannot silently clobber one another.
"""

from __future__ import annotations

import json
from typing import Any

from agents import function_tool

from ._shared import ToolConfig, require, to_thread

_BIN_STATE = "state"
_BIN_UPDATED_AT = "updated_at"

_config: ToolConfig | None = None


def configure_handoff_state(
    *,
    client: Any,
    namespace: str = "test",
    set_name: str = "handoff_state",
    default_ttl: int | None = None,
) -> None:
    """Register the Aerospike client and target set for the handoff tool."""
    global _config
    _config = ToolConfig(
        client=client,
        namespace=namespace,
        set_name=set_name,
        default_ttl=default_ttl,
    )


@function_tool
async def load_handoff_state(handoff_id: str) -> str:
    """Return the handoff state JSON for ``handoff_id``, or ``"{}"`` if none.

    Args:
        handoff_id: Stable identifier for the handoff record.
    """
    cfg = require(_config, "handoff_state")
    key = (cfg.namespace, cfg.set_name, handoff_id)

    def _read() -> str:
        try:
            _, _, bins = cfg.client.get(key)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) == 2:
                return "{}"
            raise
        value = bins.get(_BIN_STATE)
        if value is None:
            return "{}"
        return value if isinstance(value, str) else json.dumps(value)

    result = await to_thread(_read)
    return str(result)


@function_tool
async def save_handoff_state(handoff_id: str, state_json: str) -> str:
    """Write or replace the handoff state record for ``handoff_id``.

    Args:
        handoff_id: Stable identifier for the handoff record.
        state_json: JSON-encoded state payload.

    Returns:
        ``"ok"`` on success.
    """
    try:
        json.loads(state_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"state_json is not valid JSON: {e}") from e

    cfg = require(_config, "handoff_state")
    key = (cfg.namespace, cfg.set_name, handoff_id)
    import time

    meta: dict[str, Any] = {}
    if cfg.default_ttl is not None:
        meta["ttl"] = cfg.default_ttl

    def _write() -> None:
        cfg.client.put(
            key,
            {_BIN_STATE: state_json, _BIN_UPDATED_AT: int(time.time())},
            meta=meta,
        )

    await to_thread(_write)
    return "ok"
