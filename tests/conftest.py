"""Test fixtures for AerospikeSession.

All tests that need an Aerospike cluster skip automatically unless the
``AEROSPIKE_HOST`` environment variable is set. In CI this is supplied by a
service container running ``aerospike/aerospike-server`` on port 3000; for
local development, run:

    docker run -d --name aerospike -p 3000-3002:3000-3002 \\
        aerospike/aerospike-server:latest

then::

    export AEROSPIKE_HOST=127.0.0.1
    pytest
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

try:
    import aerospike  # noqa: F401 - used transitively by AerospikeSession
except ImportError:  # pragma: no cover
    aerospike = None  # type: ignore[assignment]

from openai_agents_aerospike import AerospikeSession

# Dedicated set name for tests so teardown is straightforward (scan + remove).
TEST_SET = "openai_agents_test"
TEST_NAMESPACE = os.environ.get("AEROSPIKE_NAMESPACE", "test")


def _aerospike_host() -> tuple[str, int] | None:
    """Return the (host, port) seed pair from env, or None if unset."""
    host = os.environ.get("AEROSPIKE_HOST")
    if not host:
        return None
    port = int(os.environ.get("AEROSPIKE_PORT", "3000"))
    return (host, port)


@pytest.fixture(scope="session")
def aerospike_client() -> object:
    """A shared Aerospike client for the whole test session.

    Skips all tests that request this fixture when no Aerospike server is
    reachable via ``AEROSPIKE_HOST``/``AEROSPIKE_PORT``.
    """
    if aerospike is None:
        pytest.skip("aerospike client package is not installed")
    host = _aerospike_host()
    if host is None:
        pytest.skip(
            "AEROSPIKE_HOST is not set; start an Aerospike server and export "
            "AEROSPIKE_HOST=127.0.0.1 to enable these tests"
        )

    client = aerospike.client({"hosts": [host]}).connect()
    try:
        yield client
    finally:
        try:
            client.close()
        except Exception:
            pass


@pytest.fixture
async def aerospike_session(aerospike_client: object) -> AsyncIterator[AerospikeSession]:
    """A fresh, empty AerospikeSession for a single test.

    The session uses a unique ``session_id`` and a dedicated test set, so no
    cross-test interference is possible even when tests run in parallel.
    """
    session_id = f"test-{uuid.uuid4().hex[:12]}"
    session = AerospikeSession(
        session_id=session_id,
        client=aerospike_client,
        namespace=TEST_NAMESPACE,
        set_name=TEST_SET,
        key_prefix="test",
    )
    try:
        yield session
    finally:
        try:
            await session.clear_session()
        except Exception:
            pass


def make_session(
    client: object,
    *,
    session_id: str | None = None,
    key_prefix: str = "test",
    ttl: int | None = None,
) -> AerospikeSession:
    """Helper used by tests that want more than one session at once."""
    return AerospikeSession(
        session_id=session_id or f"test-{uuid.uuid4().hex[:12]}",
        client=client,  # type: ignore[arg-type]
        namespace=TEST_NAMESPACE,
        set_name=TEST_SET,
        key_prefix=key_prefix,
        ttl=ttl,
    )
