"""Aerospike-backed Session and reference tools for the OpenAI Agents SDK.

This package provides:

- :class:`AerospikeSession`: a :class:`agents.memory.session.Session`
  implementation that persists conversation history in Aerospike.
- :class:`ShardedAerospikeSession`: opt-in variant that transparently spills
  across multiple records when a single record would exceed Aerospike's
  size limit.
- :class:`SessionRecordTooLargeError`: raised by :class:`AerospikeSession`
  when a write would exceed the record size limit.
- A small set of reference :func:`agents.function_tool` helpers for common
  Aerospike-backed agent patterns (user profiles, idempotency, handoff state,
  rate limiting).

The Session stores conversation history in a single Aerospike record per
``session_id``, using server-side atomic list operations and an optional
record TTL.

Basic usage::

    import aerospike
    from agents import Agent, Runner
    from openai_agents_aerospike import AerospikeSession

    client = aerospike.client({"hosts": [("127.0.0.1", 3000)]}).connect()

    session = AerospikeSession(
        session_id="user-123",
        client=client,
    )

    agent = Agent(name="Assistant", instructions="Reply concisely.")
    result = await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

from .session import AerospikeSession, SessionRecordTooLargeError
from .sharded_session import ShardedAerospikeSession

__all__ = [
    "AerospikeSession",
    "SessionRecordTooLargeError",
    "ShardedAerospikeSession",
]
__version__ = "0.1.0"
