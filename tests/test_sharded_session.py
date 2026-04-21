"""Tests for :class:`ShardedAerospikeSession`.

Each test either forces an overflow with an oversized payload or asserts
behavior that is distinctive to the sharded variant (multi-shard reads,
cross-shard pop, multi-record clear). They all require a live Aerospike
server via the same ``AEROSPIKE_HOST`` gating used by the rest of the suite.
"""

from __future__ import annotations

import uuid

import pytest
from agents import TResponseInputItem

from openai_agents_aerospike import ShardedAerospikeSession

from .conftest import TEST_NAMESPACE, TEST_SET

pytestmark = pytest.mark.asyncio

# Size tuned to fit in a single Aerospike record alone, but overflow the
# default 1 MiB write-block-size when two are added to the same record.
_BIG_PAYLOAD_SIZE = 700_000


def _make_sharded(client: object, *, session_id: str | None = None) -> ShardedAerospikeSession:
    return ShardedAerospikeSession(
        session_id=session_id or f"sharded-{uuid.uuid4().hex[:12]}",
        client=client,  # type: ignore[arg-type]
        namespace=TEST_NAMESPACE,
        set_name=TEST_SET,
        key_prefix="test-sharded",
    )


async def test_sharded_single_shard_roundtrip(aerospike_client: object) -> None:
    """When nothing ever overflows, a sharded session behaves like the single-record one."""
    session = _make_sharded(aerospike_client)
    try:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        await session.add_items(items)

        assert await session.active_shard() == 0
        retrieved = await session.get_items()
        assert [i.get("content") for i in retrieved] == ["hello", "hi"]
    finally:
        await session.clear_session()


async def test_sharded_overflow_rotates_to_new_shard(aerospike_client: object) -> None:
    """A write that doesn't fit in the active shard must spill to a new one."""
    session = _make_sharded(aerospike_client)
    try:
        big_a = "a" * _BIG_PAYLOAD_SIZE
        big_b = "b" * _BIG_PAYLOAD_SIZE

        # First big item fits alone on shard 0.
        await session.add_items([{"role": "user", "content": big_a}])
        assert await session.active_shard() == 0

        # Second big item would push shard 0 past the record size limit;
        # the session must rotate to shard 1 and succeed.
        await session.add_items([{"role": "assistant", "content": big_b}])
        assert await session.active_shard() >= 1

        retrieved = await session.get_items()
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == big_a
        assert retrieved[1].get("content") == big_b
    finally:
        await session.clear_session()


async def test_sharded_reads_concatenate_across_shards(aerospike_client: object) -> None:
    """Reads must concatenate messages in shard order across a rotation."""
    session = _make_sharded(aerospike_client)
    try:
        big_a = "a" * _BIG_PAYLOAD_SIZE
        big_b = "b" * _BIG_PAYLOAD_SIZE

        # Two big items force a rotation; a small item afterward lands on shard 1.
        await session.add_items([{"role": "user", "content": big_a}])
        await session.add_items([{"role": "assistant", "content": big_b}])
        await session.add_items([{"role": "user", "content": "tail"}])

        assert await session.active_shard() >= 1

        retrieved = await session.get_items()
        assert [i.get("role") for i in retrieved] == ["user", "assistant", "user"]
        assert [i.get("content") for i in retrieved] == [big_a, big_b, "tail"]

        # Limit operates on the concatenated view (tail of full list).
        last_two = await session.get_items(limit=2)
        assert [i.get("content") for i in last_two] == [big_b, "tail"]
    finally:
        await session.clear_session()


async def test_sharded_pop_walks_tail_shards(aerospike_client: object) -> None:
    """pop_item must return the final item and contract shards as they empty."""
    session = _make_sharded(aerospike_client)
    try:
        big_a = "a" * _BIG_PAYLOAD_SIZE
        big_b = "b" * _BIG_PAYLOAD_SIZE

        await session.add_items([{"role": "user", "content": big_a}])
        await session.add_items([{"role": "assistant", "content": big_b}])
        await session.add_items([{"role": "user", "content": "tail"}])

        assert await session.active_shard() >= 1

        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("content") == "tail"

        popped2 = await session.pop_item()
        assert popped2 is not None
        assert popped2.get("content") == big_b

        popped3 = await session.pop_item()
        assert popped3 is not None
        assert popped3.get("content") == big_a

        assert await session.pop_item() is None
    finally:
        await session.clear_session()


async def test_sharded_clear_removes_all_shards(aerospike_client: object) -> None:
    """clear_session must remove every shard, not just shard 0."""
    session = _make_sharded(aerospike_client)
    try:
        big_a = "a" * _BIG_PAYLOAD_SIZE
        big_b = "b" * _BIG_PAYLOAD_SIZE

        await session.add_items([{"role": "user", "content": big_a}])
        await session.add_items([{"role": "assistant", "content": big_b}])
        assert await session.active_shard() >= 1

        await session.clear_session()

        assert await session.get_items() == []
        assert await session.active_shard() == 0
    finally:
        # Second clear must be a no-op on an already-empty session.
        await session.clear_session()


async def test_sharded_limit_zero_is_noop(aerospike_client: object) -> None:
    session = _make_sharded(aerospike_client)
    try:
        await session.add_items([{"role": "user", "content": "a"}])
        assert await session.get_items(limit=0) == []
    finally:
        await session.clear_session()


async def test_sharded_unicode_across_shards(aerospike_client: object) -> None:
    """Unicode must survive the multi-shard round trip unchanged."""
    session = _make_sharded(aerospike_client)
    try:
        strings = ["こんにちは", "😊👍", "Привет", "O'Reilly"]
        for s in strings:
            await session.add_items([{"role": "user", "content": s}])
        # Force at least one shard rotation.
        await session.add_items([{"role": "assistant", "content": "x" * _BIG_PAYLOAD_SIZE}])
        await session.add_items([{"role": "assistant", "content": "y" * _BIG_PAYLOAD_SIZE}])

        retrieved = await session.get_items()
        contents = [i.get("content") for i in retrieved]
        for s in strings:
            assert s in contents
    finally:
        await session.clear_session()
