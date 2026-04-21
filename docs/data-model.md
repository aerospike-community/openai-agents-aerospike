# Data model

## Record layout

A single Aerospike record represents an entire conversation. Its key is:

```
(namespace, set_name, f"{key_prefix}:{session_id}")
```

Defaults: `namespace="test"`, `set_name="agents_sessions"`, `key_prefix="agents:session"`.

The record holds five bins:

| Bin | Type | Purpose |
|---|---|---|
| `session_id` | string | The session identifier, duplicated into the record for introspection via `asadm`. |
| `created_at` | int | Unix epoch seconds; written on every `add_items` call. |
| `updated_at` | int | Unix epoch seconds; refreshed on every `add_items` call. |
| `counter` | int | Atomic monotonic counter; incremented by `len(items)` on every `add_items`. Exposed via `_get_next_id()` for tooling. |
| `messages` | ordered list of strings | JSON-serialized input items, appended server-side via `list_append_items`. |

## Operation mapping

| Session method | Aerospike operation |
|---|---|
| `add_items(items)` | `operate()` with `write(session_id)`, `write(created_at)`, `write(updated_at)`, `increment(counter)`, `list_append_items(messages, [...])` — **single multi-op, atomic per record** |
| `get_items(limit=N)` | `operate()` with `list_get_range(messages, -N, N)` — one round trip |
| `get_items()` | `operate()` with `list_get_range(messages, 0, 2**31-1)` |
| `pop_item()` | `operate()` with `list_pop(messages, -1)` — atomically removes and returns the tail |
| `clear_session()` | `remove()` — deletes the record entirely |

Every hot-path call is a **single network round trip** to a **single record**, which means cross-process atomicity is provided by Aerospike itself. The in-process `asyncio.Lock` exists only to serialize concurrent calls on the *same* `AerospikeSession` instance, keeping the ordering predictable when a single process fans work out across tasks.

## TTL semantics

`AerospikeSession(..., ttl=N)` sets Aerospike's record-level TTL to `N` seconds and refreshes it on every `add_items` / `pop_item` call (i.e., every write path). Reads do not refresh the TTL.

Special values:

| Value | Meaning |
|---|---|
| `None` | Use the namespace default TTL (Aerospike's default behavior). |
| `0` | "Don't change the existing TTL" on update. |
| `-1` | Never expire. |
| `-2` | Reset to the namespace default. |

See the [Aerospike TTL documentation](https://aerospike.com/docs/server/guide/data-types/record#ttl) for the full semantics.

## Size limits

By default, an Aerospike record is capped at **1 MiB**. For the session data model, that means the combined size of all JSON-serialized messages plus metadata bins must stay under 1 MiB. Practical implications:

- A single large tool output (e.g., a 500 KB retrieval result) can push a record close to the limit.
- At typical chat message sizes (1–5 KB), you have headroom for ~200–1000 turns per session before compaction becomes necessary.

Options for longer conversations:

1. **Wrap with `OpenAIResponsesCompactionSession`** from the SDK's extensions. It compacts older history on a configurable threshold, shrinking the Aerospike record before it hits the limit.
2. **Switch to `ShardedAerospikeSession`** (see [Handling overflow](#handling-overflow) below) to transparently spill across multiple records while keeping reads to a single round trip.
3. **Raise the limit** in your Aerospike namespace config (`write-block-size` up to 8 MiB).

## Handling overflow

`AerospikeSession` translates Aerospike's `RecordTooBig` error into a typed `SessionRecordTooLargeError` so callers can react without parsing driver error codes:

```python
from openai_agents_aerospike import AerospikeSession, SessionRecordTooLargeError

try:
    await session.add_items(items)
except SessionRecordTooLargeError as e:
    # e.session_id, e.attempted_payload_bytes are available on the exception
    ...
```

The exception's message points at the four mitigations listed above. If you want the session to handle overflow itself rather than raise, use `ShardedAerospikeSession`:

```python
from openai_agents_aerospike import ShardedAerospikeSession

session = ShardedAerospikeSession(
    session_id="user-123",
    client=client,
    ttl=3600,
)
```

`ShardedAerospikeSession` is a drop-in subclass with the same constructor surface. On disk, shard 0 lives at the original key and shards 1+ live at `{key_prefix}:{session_id}:shard-{n}`, so existing sessions are compatible with no migration. Writes append to the active shard; if a write would overflow, the session atomically bumps the active-shard pointer on shard 0 (via `ops.increment`, so concurrent overflows get distinct shards) and retries. Reads fan out across all shards in one `get_many` round trip and concatenate the results in shard order.

The trade vs. the single-record backend: each shard is atomic per-record, but shard *transitions* are not. Under concurrent `add_items` from multiple processes racing on overflow, items from different calls may interleave at shard boundaries. Items within a single `add_items` call always stay contiguous on one shard. For single-worker-per-session workflows — the typical agent case — this is not observable.

## Why one record per session

A session could be modeled as several related records (metadata, message list, counters) or collapsed into a single record with multiple bins. We chose the single-record design because Aerospike's `operate()` API can apply any mix of bin writes, list operations, and counter increments to one record atomically in a single round trip. Benefits:

- One TTL governs the whole session, so expiration is consistent across metadata and messages.
- Multi-bin updates do not need a pipeline or a cross-record transaction — `operate()` is per-record atomic by construction.
- One round trip per Session protocol operation keeps tail latency predictable.

The tradeoff, discussed above, is the 1 MiB default record size ceiling. For conversational agents that is usually acceptable; the SDK's compaction session wrapper handles the overflow case, and raising `write-block-size` handles the rest.
