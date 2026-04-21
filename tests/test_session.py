"""Conformance tests for :class:`AerospikeSession`.

These exercise the behaviors required by the Agents SDK ``Session`` protocol:
append/read ordering, limit handling, isolation between sessions, pop/clear
semantics, concurrency safety, and TTL-free operation. They require a live
Aerospike server (see ``conftest.py``).
"""

from __future__ import annotations

import asyncio

import pytest
from agents import TResponseInputItem

from openai_agents_aerospike import AerospikeSession

from .conftest import make_session

pytestmark = pytest.mark.asyncio


async def test_add_and_get(aerospike_session: AerospikeSession) -> None:
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await aerospike_session.add_items(items)

    retrieved = await aerospike_session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"


async def test_pop_item(aerospike_session: AerospikeSession) -> None:
    await aerospike_session.add_items(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
    )
    popped = await aerospike_session.pop_item()
    assert popped is not None
    assert popped.get("content") == "second"

    remaining = await aerospike_session.get_items()
    assert len(remaining) == 1
    assert remaining[0].get("content") == "first"


async def test_pop_from_empty_returns_none(aerospike_session: AerospikeSession) -> None:
    assert await aerospike_session.pop_item() is None


async def test_clear_session(aerospike_session: AerospikeSession) -> None:
    await aerospike_session.add_items([{"role": "user", "content": "hi"}])
    assert len(await aerospike_session.get_items()) == 1

    await aerospike_session.clear_session()
    assert await aerospike_session.get_items() == []


async def test_add_empty_list_is_noop(aerospike_session: AerospikeSession) -> None:
    await aerospike_session.add_items([])
    assert await aerospike_session.get_items() == []


async def test_get_items_on_missing_record(aerospike_session: AerospikeSession) -> None:
    """get_items must return [] (not raise) when the record does not exist."""
    assert await aerospike_session.get_items() == []


async def test_limit_parameter(aerospike_session: AerospikeSession) -> None:
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "assistant", "content": "4"},
    ]
    await aerospike_session.add_items(items)

    latest_2 = await aerospike_session.get_items(limit=2)
    assert [i.get("content") for i in latest_2] == ["3", "4"]

    all_items = await aerospike_session.get_items()
    assert len(all_items) == 4

    more_than_all = await aerospike_session.get_items(limit=10)
    assert len(more_than_all) == 4

    assert await aerospike_session.get_items(limit=0) == []


async def test_session_isolation(aerospike_client: object) -> None:
    """Two sessions with different IDs must not see each other's data."""
    s1 = make_session(aerospike_client, session_id="iso-1")
    s2 = make_session(aerospike_client, session_id="iso-2")
    try:
        await s1.add_items([{"role": "user", "content": "only-in-1"}])
        await s2.add_items([{"role": "user", "content": "only-in-2"}])

        items1 = await s1.get_items()
        items2 = await s2.get_items()
        assert [i.get("content") for i in items1] == ["only-in-1"]
        assert [i.get("content") for i in items2] == ["only-in-2"]
    finally:
        await s1.clear_session()
        await s2.clear_session()


async def test_key_prefix_isolation(aerospike_client: object) -> None:
    """Same session_id under different prefixes must be isolated."""
    s1 = make_session(aerospike_client, session_id="shared-id", key_prefix="app1")
    s2 = make_session(aerospike_client, session_id="shared-id", key_prefix="app2")
    try:
        await s1.add_items([{"role": "user", "content": "app1"}])
        await s2.add_items([{"role": "user", "content": "app2"}])

        assert [i.get("content") for i in await s1.get_items()] == ["app1"]
        assert [i.get("content") for i in await s2.get_items()] == ["app2"]
    finally:
        await s1.clear_session()
        await s2.clear_session()


async def test_unicode_and_special_characters(aerospike_session: AerospikeSession) -> None:
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "😊👍"},
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": "O'Reilly"},
        {"role": "user", "content": '{"nested": "json"}'},
        {"role": "assistant", "content": "Line1\nLine2\tTabbed"},
    ]
    await aerospike_session.add_items(items)

    retrieved = await aerospike_session.get_items()
    assert [i.get("content") for i in retrieved] == [i.get("content") for i in items]


async def test_concurrent_adds_preserve_all_items(aerospike_session: AerospikeSession) -> None:
    """Concurrent add_items calls must never lose items."""

    async def add_batch(start: int, count: int) -> None:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"msg-{start + i}"} for i in range(count)
        ]
        await aerospike_session.add_items(items)

    await asyncio.gather(add_batch(0, 5), add_batch(5, 5), add_batch(10, 5))

    retrieved = await aerospike_session.get_items()
    contents = {i.get("content") for i in retrieved}
    assert contents == {f"msg-{i}" for i in range(15)}


async def test_get_next_id_is_monotonic(aerospike_session: AerospikeSession) -> None:
    a = await aerospike_session._get_next_id()
    b = await aerospike_session._get_next_id()
    c = await aerospike_session._get_next_id()
    assert a < b < c


async def test_ping(aerospike_session: AerospikeSession) -> None:
    assert await aerospike_session.ping() is True


async def test_session_settings_default(aerospike_session: AerospikeSession) -> None:
    from agents.memory import SessionSettings

    assert isinstance(aerospike_session.session_settings, SessionSettings)
    assert aerospike_session.session_settings.limit is None


async def test_session_settings_limit_used_as_default(aerospike_client: object) -> None:
    from agents.memory import SessionSettings

    session = make_session(aerospike_client, session_id="settings-test")
    session.session_settings = SessionSettings(limit=3)
    try:
        await session.add_items([{"role": "user", "content": f"m{i}"} for i in range(5)])

        default_items = await session.get_items()
        assert [i.get("content") for i in default_items] == ["m2", "m3", "m4"]

        # Explicit limit overrides session_settings.
        explicit = await session.get_items(limit=2)
        assert [i.get("content") for i in explicit] == ["m3", "m4"]
    finally:
        await session.clear_session()


async def test_external_client_not_closed(aerospike_client: object) -> None:
    """close() must be a no-op for externally-managed clients."""
    session = make_session(aerospike_client, session_id="external-client")
    try:
        await session.add_items([{"role": "user", "content": "hi"}])
        await session.close()

        # Client must still be usable after session.close() since we don't own it.
        session2 = make_session(aerospike_client, session_id="external-client-2")
        await session2.add_items([{"role": "user", "content": "still alive"}])
        assert len(await session2.get_items()) == 1
        await session2.clear_session()
    finally:
        await session.clear_session()
