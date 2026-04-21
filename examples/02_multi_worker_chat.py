"""Multi-worker example: two processes share the same session via Aerospike.

Launch two instances of this script pointing at the same ``session_id`` and
watch them take alternating turns using a shared conversation history.

    # Terminal 1
    AEROSPIKE_HOST=127.0.0.1 WORKER=A python examples/02_multi_worker_chat.py

    # Terminal 2
    AEROSPIKE_HOST=127.0.0.1 WORKER=B python examples/02_multi_worker_chat.py

Each worker reads the full history on every turn, appends its own turn, and
sleeps briefly before repeating. Because the Session protocol is stateless
from the worker's perspective -- all state lives in Aerospike -- horizontal
scale is a matter of spinning up more workers.
"""

from __future__ import annotations

import asyncio
import os

import aerospike
from agents import Agent, Runner

from openai_agents_aerospike import AerospikeSession


async def main() -> None:
    worker = os.environ.get("WORKER", "A")
    client = aerospike.client(
        {"hosts": [(os.environ.get("AEROSPIKE_HOST", "127.0.0.1"), 3000)]},
    ).connect()

    agent = Agent(name=f"Worker-{worker}", instructions="Reply in one sentence.")
    session = AerospikeSession(
        session_id="multi-worker-demo",
        client=client,
        ttl=600,
    )

    try:
        for turn in range(3):
            result = await Runner.run(
                agent,
                f"[worker={worker}] Share one interesting fact about number {turn}.",
                session=session,
            )
            print(f"[worker={worker}] turn {turn}: {result.final_output}")
            await asyncio.sleep(2)
    finally:
        await session.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
