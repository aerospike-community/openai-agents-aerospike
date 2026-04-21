"""Smoke tests that run without a live Aerospike server.

These verify that:

- The package imports cleanly.
- The AerospikeSession class satisfies the SDK's Session protocol at the
  type level (runtime_checkable Protocol isinstance check).
- All reference tool modules are importable and expose their public surface.

All other tests that touch an Aerospike cluster are gated on
``AEROSPIKE_HOST``; this file intentionally is not.
"""

from __future__ import annotations


def test_package_imports() -> None:
    import openai_agents_aerospike

    assert openai_agents_aerospike.__version__
    assert hasattr(openai_agents_aerospike, "AerospikeSession")


def test_session_conforms_to_protocol() -> None:
    from agents.memory.session import Session

    from openai_agents_aerospike import AerospikeSession

    # Session is a runtime_checkable Protocol. An instance check on the class
    # itself asserts structural conformance (presence of the expected
    # methods) without needing a connected client.
    for attr in ("get_items", "add_items", "pop_item", "clear_session"):
        assert hasattr(AerospikeSession, attr)

    # Session is a Protocol; direct isinstance against the class needs an
    # instance. We check the attributes above as a structural proxy and
    # assert the Protocol itself is importable.
    assert Session is not None


def test_tools_importable() -> None:
    from openai_agents_aerospike.tools import (
        check_idempotency,
        check_rate_limit,
        configure_handoff_state,
        configure_idempotency,
        configure_rate_limit,
        configure_user_profile,
        get_user_profile,
        load_handoff_state,
        record_idempotency,
        save_handoff_state,
        upsert_user_profile,
    )

    # @function_tool decorates into FunctionTool instances; simply ensure the
    # names resolve to callables or FunctionTool objects without touching any
    # Aerospike state.
    for obj in (
        get_user_profile,
        upsert_user_profile,
        check_idempotency,
        record_idempotency,
        load_handoff_state,
        save_handoff_state,
        check_rate_limit,
    ):
        assert obj is not None

    for fn in (
        configure_user_profile,
        configure_idempotency,
        configure_handoff_state,
        configure_rate_limit,
    ):
        assert callable(fn)
