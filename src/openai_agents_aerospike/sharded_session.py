"""Aerospike-backed Session with transparent per-session sharding.

Where :class:`~openai_agents_aerospike.AerospikeSession` stores a session in a
single record and surfaces :class:`SessionRecordTooLargeError` on overflow,
:class:`ShardedAerospikeSession` transparently spills messages across multiple
records when one fills up.

On-disk layout::

    (namespace, set_name, f"{key_prefix}:{session_id}")           # shard 0
    (namespace, set_name, f"{key_prefix}:{session_id}:shard-1")
    (namespace, set_name, f"{key_prefix}:{session_id}:shard-2")
    ...

Shard 0 is the primary record and carries all metadata bins (``session_id``,
``created_at``, ``updated_at``, ``counter``, ``active_shard``). Shards 1+ carry
only the ``messages`` list bin.

Writes target the current ``active_shard``. If the write fails with
:class:`SessionRecordTooLargeError`, the session atomically increments
``active_shard`` on shard 0 (via ``ops.increment``, so concurrent overflows get
distinct new shard numbers) and retries on the new shard.

Reads fan out across all shards in a single round trip using
:meth:`aerospike.Client.get_many`, so latency stays bounded regardless of
how many shards a long-lived session has accumulated.

Tradeoff vs. :class:`AerospikeSession`: each shard is atomic per-record, but
shard *transitions* are not. Under concurrent ``add_items`` calls from
multiple processes racing on overflow, items from different calls may
interleave at shard boundaries. Items within a single ``add_items`` call stay
contiguous on a single shard. For typical single-worker-per-session workflows
this is not observable.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aerospike_helpers.operations import list_operations as list_ops, operations as ops
from agents.items import TResponseInputItem
from agents.memory.session_settings import resolve_session_limit

from .session import (
    _BIN_COUNTER,
    _BIN_CREATED_AT,
    _BIN_MESSAGES,
    _BIN_SESSION_ID,
    _BIN_UPDATED_AT,
    AerospikeSession,
    SessionRecordTooLargeError,
    _is_record_too_big,
)

_BIN_ACTIVE_SHARD = "active_shard"


class ShardedAerospikeSession(AerospikeSession):
    """``AerospikeSession`` variant that transparently shards across records.

    Opt in when session size is unbounded and you cannot rely on
    ``OpenAIResponsesCompactionSession`` compaction (for example, because
    you need full-fidelity replay of every item). The constructor signature
    matches :class:`AerospikeSession`; existing single-record sessions are
    compatible as shard 0 of a sharded session with no data migration.

    Example::

        client = aerospike.client({"hosts": [("127.0.0.1", 3000)]}).connect()
        session = ShardedAerospikeSession(
            session_id="user-123",
            client=client,
            ttl=3600,
        )
    """

    def _shard_key(self, shard: int) -> tuple[str, str, str]:
        """Return the Aerospike record key tuple for a given shard number."""
        if shard == 0:
            return self._record_key
        return (
            self._namespace,
            self._set_name,
            f"{self._key_prefix}:{self.session_id}:shard-{shard}",
        )

    # ------------------------------------------------------------------
    # Active-shard bookkeeping
    # ------------------------------------------------------------------

    def _read_active_shard(self) -> int:
        """Read the current active-shard pointer from shard 0, or 0 if missing."""
        try:
            _, _, bins = self._client.operate(self._record_key, [ops.read(_BIN_ACTIVE_SHARD)])
        except Exception as exc:  # noqa: BLE001
            if self._handle_missing_record(exc):
                return 0
            raise
        value = bins.get(_BIN_ACTIVE_SHARD)
        return int(value) if value is not None else 0

    def _bump_active_shard(self) -> int:
        """Atomically increment and return the new active-shard number.

        Uses ``ops.increment`` + ``ops.read`` in one ``operate()``, so every
        caller that races on overflow receives a distinct new shard number
        without needing generation-based CAS.
        """
        _, _, bins = self._client.operate(
            self._record_key,
            [ops.increment(_BIN_ACTIVE_SHARD, 1), ops.read(_BIN_ACTIVE_SHARD)],
            meta=self._write_meta(),
        )
        value = bins.get(_BIN_ACTIVE_SHARD)
        return int(value) if value is not None else 1

    def _decrement_active_shard(self) -> int:
        """Decrement the active-shard pointer by 1, clamped at 0.

        Used by :meth:`pop_item` when the tail shard empties out. This is a
        read-modify-write rather than an atomic op because we must clamp at
        zero; contention is rare (only fires when a shard empties), so a
        simple last-writer-wins is acceptable.
        """
        current = self._read_active_shard()
        if current <= 0:
            return 0
        new_value = current - 1
        self._client.operate(
            self._record_key,
            [ops.write(_BIN_ACTIVE_SHARD, new_value)],
            meta=self._write_meta(),
        )
        return new_value

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _add_items_sync(self, serialized: list[str], now: int) -> None:
        """Append items to the active shard, rotating on overflow.

        Metadata bins (updated_at, counter, etc.) always live on shard 0.
        When the active shard is 0 we fuse the metadata update and the list
        append into a single ``operate()``; otherwise we write metadata to
        shard 0 and messages to shard N in two separate round trips.
        """
        active = self._read_active_shard()

        meta_ops = [
            ops.write(_BIN_SESSION_ID, self.session_id),
            ops.write(_BIN_UPDATED_AT, now),
            ops.write(_BIN_CREATED_AT, now),
            ops.increment(_BIN_COUNTER, len(serialized)),
        ]

        if active == 0:
            combined = meta_ops + [list_ops.list_append_items(_BIN_MESSAGES, serialized)]
            try:
                self._client.operate(self._record_key, combined, meta=self._write_meta())
                return
            except Exception as exc:  # noqa: BLE001
                if not _is_record_too_big(exc):
                    raise
                # Shard 0 is full. Allocate shard 1, write messages there,
                # and refresh shard 0's metadata in a separate call.
                new_shard = self._bump_active_shard()
                self._append_to_shard(new_shard, serialized)
                self._client.operate(self._record_key, meta_ops, meta=self._write_meta())
                return

        # Active > 0: update metadata on shard 0, then append to the tail shard.
        self._client.operate(self._record_key, meta_ops, meta=self._write_meta())
        try:
            self._append_to_shard(active, serialized)
        except SessionRecordTooLargeError:
            new_shard = self._bump_active_shard()
            self._append_to_shard(new_shard, serialized)

    def _append_to_shard(self, shard: int, serialized: list[str]) -> None:
        """Append items to the ``messages`` bin of a specific shard record.

        Translates Aerospike's RecordTooBig into
        :class:`SessionRecordTooLargeError` so the caller can react.
        """
        try:
            self._client.operate(
                self._shard_key(shard),
                [list_ops.list_append_items(_BIN_MESSAGES, serialized)],
                meta=self._write_meta(),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_record_too_big(exc):
                raise SessionRecordTooLargeError(
                    self.session_id,
                    attempted_payload_bytes=sum(len(s) for s in serialized),
                    cause=exc,
                ) from exc
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        session_limit = resolve_session_limit(limit, self.session_settings)
        if session_limit is not None and session_limit <= 0:
            return []

        async with self._lock:
            raw_messages = await asyncio.to_thread(self._get_items_sharded_sync, session_limit)

        items: list[TResponseInputItem] = []
        for raw in raw_messages:
            try:
                if isinstance(raw, bytes):
                    items.append(await self._deserialize_item(raw.decode("utf-8")))
                elif isinstance(raw, str):
                    items.append(await self._deserialize_item(raw))
                elif isinstance(raw, dict):
                    items.append(raw)  # type: ignore[arg-type]
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return items

    def _get_items_sharded_sync(self, session_limit: int | None) -> list[Any]:
        """Fan-out read across all shards in one ``batch_read`` round trip.

        We read every shard's ``messages`` list in full, concatenate in
        shard order, and slice client-side when ``session_limit`` is set.
        This keeps the implementation straightforward while preserving the
        single-round-trip latency profile. A tail-first optimization that
        reads only enough shards to satisfy ``session_limit`` is possible
        but unnecessary until real-world shard counts grow significantly
        beyond a handful.
        """
        active = self._read_active_shard()
        if active == 0:
            return super()._get_items_sync(session_limit)

        keys = [self._shard_key(n) for n in range(active + 1)]
        batch = self._client.batch_read(keys, [_BIN_MESSAGES])

        # batch_read results are not guaranteed to come back in input-key order,
        # so index them by the user-key component of the Aerospike key tuple
        # and then walk shards 0..active to get deterministic insertion order.
        by_user_key: dict[str, list[Any]] = {}
        for br in batch.batch_records:
            if br.result != 0 or not br.record:
                continue
            record_key = br.key
            if not record_key or len(record_key) < 3:
                continue
            user_key = record_key[2]
            record_bins = br.record[2] if len(br.record) >= 3 else {}
            raw = record_bins.get(_BIN_MESSAGES) if record_bins else None
            if raw:
                by_user_key[user_key] = list(raw)

        collected: list[Any] = []
        for shard in range(active + 1):
            shard_user_key = self._shard_key(shard)[2]
            collected.extend(by_user_key.get(shard_user_key, []))

        if session_limit is not None and len(collected) > session_limit:
            collected = collected[-session_limit:]
        return collected

    # ------------------------------------------------------------------
    # Pop
    # ------------------------------------------------------------------

    def _pop_item_sync(self) -> Any:
        """Pop the tail item, contracting shards as they empty.

        Walks from the active shard toward shard 0, popping the first
        non-empty shard encountered. When a shard empties we decrement the
        active-shard pointer so future reads and pops stop visiting it.
        The emptied shard record itself is left in place; it is removed
        only by :meth:`clear_session`.
        """
        active = self._read_active_shard()
        import aerospike

        exc_module = getattr(aerospike, "exception", None)
        op_not_applicable = getattr(exc_module, "OpNotApplicable", None) if exc_module else None

        for shard in range(active, -1, -1):
            try:
                _, _, bins = self._client.operate(
                    self._shard_key(shard),
                    [list_ops.list_pop(_BIN_MESSAGES, -1)],
                    meta=self._write_meta(),
                )
                popped = bins.get(_BIN_MESSAGES)
                if popped is not None:
                    return popped
                # Shard exists but yielded nothing; fall through to decrement.
            except Exception as exc:  # noqa: BLE001
                if self._handle_missing_record(exc):
                    pass  # Shard never existed; try the next one down.
                elif op_not_applicable is not None and isinstance(exc, op_not_applicable):
                    pass  # Empty list on this shard; contract pointer, keep going.
                else:
                    raise

            if shard > 0:
                self._decrement_active_shard()
        return None

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------

    async def clear_session(self) -> None:
        """Remove every shard record for this session."""

        def _op() -> None:
            active = self._read_active_shard()
            keys = [self._shard_key(n) for n in range(active + 1)]
            if len(keys) == 1:
                try:
                    self._client.remove(keys[0])
                except Exception as exc:  # noqa: BLE001
                    if self._handle_missing_record(exc):
                        return
                    raise
                return

            # batch_remove is a single network round trip and ignores
            # missing records, which is the behavior we want here.
            batch_remove = getattr(self._client, "batch_remove", None)
            if batch_remove is not None:
                batch_remove(keys)
                return

            # Fallback: older clients without batch_remove. Remove each key
            # sequentially; missing records are swallowed.
            for key in keys:
                try:
                    self._client.remove(key)
                except Exception as exc:  # noqa: BLE001
                    if not self._handle_missing_record(exc):
                        raise

        async with self._lock:
            await asyncio.to_thread(_op)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def active_shard(self) -> int:
        """Return the current active-shard number (0 means single-record)."""
        return await asyncio.to_thread(self._read_active_shard)


__all__ = ["ShardedAerospikeSession"]
