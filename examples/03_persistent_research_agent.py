"""Persistent research agent: Session + user_profile tool + idempotency.

Demonstrates combining :class:`AerospikeSession` with the reference tools
for a single agent that:

- Reads the calling user's profile before responding.
- Persists its conversation history across process restarts.
- Uses idempotency keys to avoid repeating expensive tool work on retries.
"""

from __future__ import annotations

import asyncio
import json
import os

import aerospike
from agents import Agent, Runner

from openai_agents_aerospike import AerospikeSession
from openai_agents_aerospike.tools import (
    check_idempotency,
    configure_idempotency,
    configure_user_profile,
    get_user_profile,
    record_idempotency,
    upsert_user_profile,
)


async def main() -> None:
    client = aerospike.client(
        {"hosts": [(os.environ.get("AEROSPIKE_HOST", "127.0.0.1"), 3000)]},
    ).connect()

    configure_user_profile(client=client, namespace="test", set_name="user_profiles")
    configure_idempotency(client=client, namespace="test", set_name="idempotency")

    # Seed a profile so the agent has something to look up.
    profile_key = ("test", "user_profiles", "demo-user")
    client.put(profile_key, {"profile": json.dumps({"name": "Ada", "interests": ["graphs"]})})

    agent = Agent(
        name="ResearchAssistant",
        instructions=(
            "You are a research assistant. Always call get_user_profile first, "
            "use check_idempotency before any tool that causes side effects, and "
            "call record_idempotency after a successful side-effecting tool."
        ),
        tools=[
            get_user_profile,
            upsert_user_profile,
            check_idempotency,
            record_idempotency,
        ],
    )

    session = AerospikeSession(session_id="demo-user:research", client=client, ttl=7 * 24 * 3600)

    try:
        result = await Runner.run(
            agent,
            "Hi, I'd like a 2-sentence intro about a topic I'd enjoy.",
            session=session,
        )
        print(result.final_output)
    finally:
        await session.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
