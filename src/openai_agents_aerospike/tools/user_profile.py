"""User-profile tool: read/write a per-user JSON blob in Aerospike.

Typical data layout::

    (namespace, set="user_profiles", key=user_id) -> { "profile": "<json>" }

The tool exposes ``get_user_profile`` and ``upsert_user_profile`` for the
agent to call. Both accept ``user_id: str`` as their primary identifier.
"""

from __future__ import annotations

import json
from typing import Any

from agents import function_tool

from ._shared import ToolConfig, require, to_thread

_BIN_PROFILE = "profile"

_config: ToolConfig | None = None


def configure_user_profile(
    *,
    client: Any,
    namespace: str = "test",
    set_name: str = "user_profiles",
    default_ttl: int | None = None,
) -> None:
    """Register the Aerospike client and target set for the user-profile tool.

    Call this once from your application startup before handing the tool to
    an agent.
    """
    global _config
    _config = ToolConfig(
        client=client,
        namespace=namespace,
        set_name=set_name,
        default_ttl=default_ttl,
    )


@function_tool
async def get_user_profile(user_id: str) -> str:
    """Return the JSON-encoded profile for ``user_id``, or ``"{}"`` if none.

    Args:
        user_id: The stable identifier for the user whose profile to read.
    """
    cfg = require(_config, "user_profile")
    key = (cfg.namespace, cfg.set_name, user_id)

    def _read() -> str:
        try:
            _, _, bins = cfg.client.get(key)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) == 2:
                return "{}"
            raise
        value = bins.get(_BIN_PROFILE)
        if value is None:
            return "{}"
        return value if isinstance(value, str) else json.dumps(value)

    result = await to_thread(_read)
    return str(result)


@function_tool
async def upsert_user_profile(user_id: str, profile_json: str) -> str:
    """Write or replace the profile for ``user_id``.

    Args:
        user_id: The stable identifier for the user.
        profile_json: A JSON-encoded object containing the profile fields.
            Must be valid JSON; malformed input is rejected.

    Returns:
        ``"ok"`` on success.
    """
    try:
        json.loads(profile_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"profile_json is not valid JSON: {e}") from e

    cfg = require(_config, "user_profile")
    key = (cfg.namespace, cfg.set_name, user_id)
    meta = {"ttl": cfg.default_ttl} if cfg.default_ttl is not None else {}

    def _write() -> None:
        cfg.client.put(key, {_BIN_PROFILE: profile_json}, meta=meta)

    await to_thread(_write)
    return "ok"
