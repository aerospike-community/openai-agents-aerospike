# Quickstart

## 1. Start Aerospike

```bash
docker run -d --name aerospike -p 3000-3002:3000-3002 aerospike/aerospike-server:latest
```

This starts an Aerospike Community Edition server with the default `test` namespace on `127.0.0.1:3000`. The `test` namespace is ephemeral (RAM-only by default in the shipped CE config), which is fine for local development. See the [production notes](#production-notes) below before using this in production.

## 2. Install

```bash
pip install "git+https://github.com/aerospike-community/openai-agents-aerospike@v0.1.0"
```

Until `AerospikeSession` is available from an upstream `openai-agents` release, install directly from this repository against a pinned tag.

## 3. Minimal session

```python
import asyncio
import aerospike
from agents import Agent, Runner
from openai_agents_aerospike import AerospikeSession

async def main() -> None:
    client = aerospike.client({"hosts": [("127.0.0.1", 3000)]}).connect()
    session = AerospikeSession(session_id="user-123", client=client, ttl=3600)

    agent = Agent(name="Assistant", instructions="Reply in one sentence.")

    result = await Runner.run(agent, "Hello", session=session)
    print(result.final_output)

    await session.close()
    client.close()

asyncio.run(main())
```

The Session protocol is the same regardless of backend — `Runner.run(..., session=session)` does not care which implementation is behind the object, so swapping `AerospikeSession` in for another backend is a local change at session construction. For a backend-specific migration walkthrough, see [`migration-from-redis.md`](migration-from-redis.md).

## Production notes

- Create a dedicated namespace (e.g. `agents`) in your Aerospike cluster configuration rather than reusing `test`. Pass it via `namespace="agents"`.
- Choose a `ttl` that matches your retention policy. Pass `ttl=-1` to disable expiration entirely.
- Reuse a single `aerospike.Client` across requests (it maintains its own connection pool). Pass it to every `AerospikeSession` as the `client=` argument and let your application manage its lifecycle.
- Set `set_name="agents_sessions"` (the default) or whatever your application's naming convention prescribes. Avoid sharing a set between sessions and unrelated records.
