# openai-agents-aerospike

[Aerospike](https://aerospike.com/)-backed [`Session`](https://openai.github.io/openai-agents-python/sessions/) implementation and a small set of reference [`@function_tool`](https://openai.github.io/openai-agents-python/tools/) helpers for the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python).

- **`AerospikeSession`** — a `Session` implementation that persists conversation history in Aerospike, with record-level TTL, server-side atomic list operations, and single-round-trip reads and writes.
- **Reference tools** — small, LLM-friendly `@function_tool` wrappers for common patterns (user profiles, idempotency keys, handoff state, rate limiting).

The goal is to make Aerospike a first-class choice for agent memory and to contribute `AerospikeSession` upstream to [`openai/openai-agents-python`](https://github.com/openai/openai-agents-python) as `agents.extensions.memory.AerospikeSession` once it has stabilized here.

## Installation

```bash
pip install openai-agents-aerospike
```

This pulls in `openai-agents>=0.14` and `aerospike>=19.1`.

## Quick start

```python
import asyncio
import aerospike
from agents import Agent, Runner
from openai_agents_aerospike import AerospikeSession

async def main() -> None:
    client = aerospike.client({"hosts": [("127.0.0.1", 3000)]}).connect()
    session = AerospikeSession(session_id="user-123", client=client, ttl=3600)

    agent = Agent(name="Assistant", instructions="Reply concisely.")
    first  = await Runner.run(agent, "Golden Gate Bridge, which city?", session=session)
    second = await Runner.run(agent, "And which state?", session=session)
    print(first.final_output, "/", second.final_output)

    await session.close()
    client.close()

asyncio.run(main())
```

See [`examples/`](examples/) for a multi-worker chat, a persistent research agent, and an agent-to-agent handoff that shares structured state through Aerospike.

## Data model

One Aerospike record per `session_id` inside `(namespace, set_name)`:

| Bin | Type | Purpose |
|---|---|---|
| `session_id` | string | Plain identifier (duplicated from the key for introspection). |
| `created_at` | int | Unix epoch seconds; set on first write. |
| `updated_at` | int | Unix epoch seconds; refreshed on every `add_items`. |
| `counter` | int | Monotonic counter incremented atomically on each `add_items`. |
| `messages` | ordered list | JSON-serialized items, appended via `list_append_items`, popped via `list_pop(-1)`. |

All read/write hot paths use `client.operate()` for a single-round-trip, atomic multi-op.

See [`docs/data-model.md`](docs/data-model.md) for a deeper treatment, including the 1 MiB default record size and how to plan around it for long-running conversations.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Data model](docs/data-model.md)
- [Migrating from `RedisSession`](docs/migration-from-redis.md)
- [Benchmark methodology and results](docs/benchmark-results.md)

## Roadmap

Implemented today:

- `AerospikeSession` conforming to the SDK's `Session` protocol.
- Conformance test suite against a live Aerospike Community Edition server.
- CI matrix against Python 3.10 – 3.12 with an Aerospike CE service container.
- Reference `@function_tool` helpers for user profiles, idempotency, handoff state, and rate limiting.
- Runnable example agents.

Planned:

- Published latency benchmark results.
- Upstream contribution to `openai/openai-agents-python`.

## Development

```bash
# Start a local Aerospike Community Edition server
docker run -d --name aerospike -p 3000-3002:3000-3002 aerospike/aerospike-server:latest

# Install the package and dev tooling
pip install -e .
pip install pytest pytest-asyncio pytest-cov mypy "ruff==0.9.2"

# Run the test suite, linter, and type checker
export AEROSPIKE_HOST=127.0.0.1
pytest -v
ruff check .
mypy src/openai_agents_aerospike
```

Tests that require a live Aerospike cluster automatically skip when `AEROSPIKE_HOST` is unset, so the smoke tests in `tests/test_import.py` still run in a minimal environment.

## Contributing

Issues and pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to file bugs, propose changes, and run the full validation workflow locally. By participating, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
