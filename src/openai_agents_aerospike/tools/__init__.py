"""Reference ``@function_tool`` helpers for common Aerospike-backed patterns.

Each module exposes:

- A ``configure_<name>(...)`` function that registers a shared Aerospike
  client + namespace/set to be used by the tool functions.
- One or more ``@function_tool`` callables with LLM-friendly signatures
  (short docstrings, primitive parameters).

Tools are kept independent of :class:`AerospikeSession` so you can use one
without the other. A single :class:`aerospike.Client` can be shared across
the session and all tools.
"""

from __future__ import annotations

from .handoff_state import configure_handoff_state, load_handoff_state, save_handoff_state
from .idempotency import check_idempotency, configure_idempotency, record_idempotency
from .rate_limit import check_rate_limit, configure_rate_limit
from .user_profile import configure_user_profile, get_user_profile, upsert_user_profile

__all__ = [
    "check_idempotency",
    "check_rate_limit",
    "configure_handoff_state",
    "configure_idempotency",
    "configure_rate_limit",
    "configure_user_profile",
    "get_user_profile",
    "load_handoff_state",
    "record_idempotency",
    "save_handoff_state",
    "upsert_user_profile",
]
