"""Handoff example: two agents share structured state via Aerospike.

Agent A gathers requirements and writes a handoff record, then hands off to
Agent B, which reads that handoff record and continues the work. The handoff
payload is stored outside the conversation history so it survives across
multiple sessions and workers.
"""

from __future__ import annotations

import asyncio
import os

import aerospike
from agents import Agent, Runner

from openai_agents_aerospike import AerospikeSession
from openai_agents_aerospike.tools import (
    configure_handoff_state,
    load_handoff_state,
    save_handoff_state,
)


async def main() -> None:
    client = aerospike.client(
        {"hosts": [(os.environ.get("AEROSPIKE_HOST", "127.0.0.1"), 3000)]},
    ).connect()
    configure_handoff_state(client=client, namespace="test", set_name="handoff_state")

    specialist = Agent(
        name="Specialist",
        instructions=(
            "You are the specialist. Call load_handoff_state with the provided "
            "handoff_id, then act on the state you find."
        ),
        tools=[load_handoff_state],
    )

    intake = Agent(
        name="Intake",
        instructions=(
            "You gather a short problem statement from the user, call "
            "save_handoff_state(handoff_id='task-1', state_json=<json>), and "
            "then hand off to Specialist."
        ),
        tools=[save_handoff_state],
        handoffs=[specialist],
    )

    session = AerospikeSession(session_id="handoff-demo", client=client, ttl=3600)

    try:
        result = await Runner.run(
            intake,
            "I need help planning a small data-pipeline change at work. "
            "Start by capturing this as handoff task 'task-1'.",
            session=session,
        )
        print(result.final_output)
    finally:
        await session.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
