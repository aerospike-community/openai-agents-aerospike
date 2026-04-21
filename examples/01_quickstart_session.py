"""Quickstart: swap SQLiteSession for AerospikeSession.

Run a local Aerospike first::

    docker run -d --name aerospike -p 3000-3002:3000-3002 \\
        aerospike/aerospike-server:latest

Then set ``OPENAI_API_KEY`` and run this file.
"""

from __future__ import annotations

import asyncio
import os

import aerospike
from agents import Agent, Runner

from openai_agents_aerospike import AerospikeSession


async def main() -> None:
    client = aerospike.client(
        {"hosts": [(os.environ.get("AEROSPIKE_HOST", "127.0.0.1"), 3000)]},
    ).connect()

    agent = Agent(name="Assistant", instructions="Reply concisely.")

    session = AerospikeSession(
        session_id="quickstart-user-123",
        client=client,
        # The defaults target the stock dev namespace; override for production.
        namespace="test",
        set_name="agents_sessions",
        ttl=3600,  # 1 hour
    )

    try:
        first = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
        print("Turn 1:", first.final_output)

        second = await Runner.run(agent, "What state is it in?", session=session)
        print("Turn 2:", second.final_output)
    finally:
        await session.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
