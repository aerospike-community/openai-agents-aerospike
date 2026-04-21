"""Shared state for reference tool modules.

Each tool module keeps its own registered client/namespace/set tuple so that
consumers can either:

- Share one Aerospike client across all tools (by calling every
  ``configure_*`` helper with the same client), or
- Use different clusters/sets per tool (by configuring them independently).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolConfig:
    """Runtime configuration for a reference tool."""

    client: Any  # aerospike.Client; Any to keep the client import optional.
    namespace: str
    set_name: str
    default_ttl: int | None = None


def require(config: ToolConfig | None, tool_name: str) -> ToolConfig:
    """Return ``config`` or raise a helpful error if it has not been set."""
    if config is None:
        raise RuntimeError(
            f"{tool_name} has not been configured. "
            f"Call configure_{tool_name}(client=..., namespace=..., set_name=...) "
            "from your application setup before registering it as a tool."
        )
    return config


async def to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Thin wrapper around :func:`asyncio.to_thread` for consistency."""
    return await asyncio.to_thread(func, *args, **kwargs)
