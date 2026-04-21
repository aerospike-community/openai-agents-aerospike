# Migrating from `RedisSession` to `AerospikeSession`

This guide is for users already running the Agents SDK with `RedisSession` who want to switch to Aerospike. It focuses on the mechanical differences so you can plan the change with confidence; it is not a competitive comparison.

## Code changes

The `Session` protocol is identical, so `Runner.run(..., session=session)` does not change. Only session construction differs.

```python
# Before
from agents.extensions.memory import RedisSession
session = RedisSession.from_url(
    "user-123",
    url="redis://localhost:6379/0",
    key_prefix="myapp:session",
    ttl=3600,
)

# After
import aerospike
from openai_agents_aerospike import AerospikeSession

client = aerospike.client({"hosts": [("127.0.0.1", 3000)]}).connect()
session = AerospikeSession(
    session_id="user-123",
    client=client,
    key_prefix="myapp:session",
    ttl=3600,
)
```

## Parameter mapping

| `RedisSession` | `AerospikeSession` | Notes |
|---|---|---|
| `url` / `redis_client` | `client` (connected `aerospike.Client`) | Aerospike clients pool connections internally. Construct one at startup and share it across sessions and tools. |
| `key_prefix="agents:session"` | `key_prefix="agents:session"` | Same role: prepended to `session_id` when forming the storage key. |
| `ttl` | `ttl` | Same role: per-session TTL refreshed on write. |
| (N/A) | `namespace`, `set_name` | Aerospike-specific keying. Defaults target the stock CE dev namespace (`test`); create a dedicated namespace for production. |

## Behavioral notes

- **Atomicity.** `AerospikeSession` writes use `operate()`, which applies all bin writes and list appends to one record atomically. If you rely on specific ordering semantics from your current backend (for example, batching multiple `add_items` calls), re-read the relevant section of [`data-model.md`](data-model.md) to confirm the new semantics match your expectations.
- **Isolation.** `AerospikeSession` isolates sessions by the `(namespace, set_name, key_prefix)` tuple rather than by Redis logical DB. Put different applications or environments in different sets (or namespaces), and reserve `key_prefix` for per-application scoping within a set.
- **TTL.** Both backends refresh TTL on every write. Aerospike applies the TTL at the record level, so all the session's metadata and messages expire together.

## Migrating existing data

There is no in-place migration — the two backends have different on-disk layouts. In practice you have two options:

1. **Hard cutover.** Acceptable when session data is ephemeral (TTL-bound chat contexts). Deploy with `AerospikeSession`; existing Redis sessions expire naturally.
2. **Soft cutover.** Subclass `AerospikeSession` and override `get_items` to fall back to a read-only `RedisSession` when the Aerospike record is empty, while `add_items` always writes to Aerospike. Delete the Redis keys after a grace period.

A reference implementation of the soft-cutover pattern is planned for a follow-up release.
