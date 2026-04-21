"""Aerospike-powered Session backend for the OpenAI Agents SDK.

Conversation history is stored as a single Aerospike record per ``session_id``.
Message items are appended to an ordered list bin (``messages``) via the
server-side multi-op API (:py:meth:`aerospike.Client.operate`), which gives
each ``add_items`` / ``pop_item`` / ``get_items`` call single-round-trip
atomicity even across multiple processes.

The Aerospike Python client (19.x) is synchronous. To satisfy the Agents SDK's
async Session protocol, every blocking call is dispatched to a worker thread
via :func:`asyncio.to_thread`. Aerospike operations are typically sub-ms, so
the thread-offload overhead is negligible compared to the model round-trip.

Usage::

    import aerospike
    from agents import Agent, Runner
    from openai_agents_aerospike import AerospikeSession

    client = aerospike.client({"hosts": [("127.0.0.1", 3000)]}).connect()
    session = AerospikeSession(session_id="user-123", client=client)

    result = await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

try:
    import aerospike
    from aerospike_helpers.operations import list_operations as list_ops, operations as ops
except ImportError as e:  # pragma: no cover - import-time guard
    raise ImportError(
        "AerospikeSession requires the 'aerospike' package. Install it with: pip install aerospike"
    ) from e

from agents.items import TResponseInputItem
from agents.memory.session import SessionABC
from agents.memory.session_settings import SessionSettings, resolve_session_limit

if TYPE_CHECKING:
    AerospikeClient = aerospike.Client
else:
    AerospikeClient = Any


_BIN_SESSION_ID = "session_id"
_BIN_CREATED_AT = "created_at"
_BIN_UPDATED_AT = "updated_at"
_BIN_COUNTER = "counter"
_BIN_MESSAGES = "messages"

# AEROSPIKE_ERR_RECORD_TOO_BIG, returned when a write would exceed the
# namespace's configured write-block-size (default 1 MiB).
_ERR_RECORD_TOO_BIG = 13


class SessionRecordTooLargeError(Exception):
    """Raised when an ``add_items`` call would exceed Aerospike's record size limit.

    Aerospike caps each record at the namespace ``write-block-size`` setting
    (default 1 MiB). Once a session's serialized ``messages`` list plus
    metadata bins approaches that ceiling, further appends fail with this
    error.

    Mitigations, in roughly increasing disruption:

    1. Wrap the session with ``OpenAIResponsesCompactionSession`` from the
       SDK's extensions. Compacts older history transparently.
    2. Switch to :class:`openai_agents_aerospike.ShardedAerospikeSession`,
       which transparently spills across multiple records when one fills up.
    3. Raise ``write-block-size`` in the Aerospike namespace config (up to
       8 MiB).
    4. Shorten long tool outputs before persisting them.

    Attributes:
        session_id: The session that overflowed.
        attempted_payload_bytes: Approximate size in bytes of the serialized
            items that triggered the failure (upper bound on what would have
            been appended; does not include existing record contents).
    """

    def __init__(
        self,
        session_id: str,
        *,
        attempted_payload_bytes: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        msg = (
            f"Session '{session_id}' exceeded Aerospike's record size limit "
            f"(default 1 MiB). Options: wrap with OpenAIResponsesCompactionSession, "
            f"switch to ShardedAerospikeSession, or raise write-block-size."
        )
        super().__init__(msg)
        self.session_id = session_id
        self.attempted_payload_bytes = attempted_payload_bytes
        if cause is not None:
            self.__cause__ = cause


def _is_record_too_big(exc: BaseException) -> bool:
    """Return True if ``exc`` is Aerospike's record-too-big error."""
    exc_module = getattr(aerospike, "exception", None)
    record_too_big = getattr(exc_module, "RecordTooBig", None) if exc_module else None
    if record_too_big is not None and isinstance(exc, record_too_big):
        return True
    return getattr(exc, "code", None) == _ERR_RECORD_TOO_BIG


class AerospikeSession(SessionABC):
    """Aerospike implementation of :pyclass:`agents.memory.session.Session`.

    One Aerospike record per session, keyed by ``f"{key_prefix}:{session_id}"``
    inside ``(namespace, set_name)``. Conversation items are JSON-serialized
    strings stored in an ordered list bin ``messages``. A ``counter`` bin is
    incremented atomically on each ``add_items`` call for debugging and
    cross-process ordering; timestamps live in ``created_at`` / ``updated_at``.

    Record-level TTL is applied via the write policy when provided, so the
    whole session expires as a single unit.
    """

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        client: AerospikeClient,
        namespace: str = "test",
        set_name: str = "agents_sessions",
        key_prefix: str = "agents:session",
        ttl: int | None = None,
        session_settings: SessionSettings | None = None,
    ) -> None:
        """Initializes a new AerospikeSession.

        Args:
            session_id: Unique identifier for the conversation.
            client: A connected :class:`aerospike.Client` instance.
            namespace: Aerospike namespace. Defaults to ``"test"``, matching
                the out-of-the-box namespace in Aerospike's default development
                configuration. For production deployments, create a dedicated
                namespace (e.g. ``"agents"``) and pass it explicitly.
            set_name: Aerospike set name. Defaults to ``"agents_sessions"``.
            key_prefix: String prefix prepended to ``session_id`` before
                forming the Aerospike record key, to avoid collisions with
                other application data that may share the same set.
                Defaults to ``"agents:session"``.
            ttl: Optional record-level time-to-live in seconds. ``None`` means
                "use the namespace default TTL"; pass ``-1`` to disable
                expiration entirely. When set, the TTL is refreshed on every
                write path (``add_items`` / ``pop_item``).
            session_settings: Optional session configuration settings. If
                ``None``, uses a default :class:`SessionSettings()`.
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()

        self._client = client
        self._namespace = namespace
        self._set_name = set_name
        self._key_prefix = key_prefix
        self._ttl = ttl
        self._owns_client = False

        # Aerospike record key tuple: (namespace, set, user_key)
        self._record_key: tuple[str, str, str] = (
            namespace,
            set_name,
            f"{self._key_prefix}:{self.session_id}",
        )

        # Intra-process ordering guarantee. Cross-process atomicity is provided
        # by Aerospike's single-record operate() call.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_hosts(
        cls,
        session_id: str,
        *,
        hosts: list[tuple[str, int]],
        namespace: str = "test",
        set_name: str = "agents_sessions",
        aerospike_config: dict[str, Any] | None = None,
        session_settings: SessionSettings | None = None,
        **kwargs: Any,
    ) -> AerospikeSession:
        """Create a session from a list of Aerospike seed hosts.

        Args:
            session_id: Conversation ID.
            hosts: List of ``(address, port)`` tuples, e.g.
                ``[("127.0.0.1", 3000)]``.
            namespace: Aerospike namespace.
            set_name: Aerospike set name.
            aerospike_config: Optional dict merged into the Aerospike client
                config (e.g. ``{"policies": {"timeout": 1000}}``). The
                ``hosts`` key, if present, is overridden by the ``hosts``
                argument.
            session_settings: Optional session configuration settings.
            **kwargs: Forwarded to the main constructor (e.g. ``key_prefix``,
                ``ttl``).

        Returns:
            AerospikeSession: A session with an internally-owned client that
            will be closed when :meth:`close` is called.
        """
        config: dict[str, Any] = dict(aerospike_config or {})
        config["hosts"] = hosts
        client = aerospike.client(config).connect()
        session = cls(
            session_id,
            client=client,
            namespace=namespace,
            set_name=set_name,
            session_settings=session_settings,
            **kwargs,
        )
        session._owns_client = True
        return session

    # ------------------------------------------------------------------
    # Serialization helpers (overridable)
    # ------------------------------------------------------------------

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to a compact JSON string."""
        return json.dumps(item, separators=(",", ":"), ensure_ascii=False)

    async def _deserialize_item(self, raw: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item."""
        return json.loads(raw)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_meta(self) -> dict[str, Any]:
        """Metadata dict for ``operate()`` / ``put()`` calls."""
        if self._ttl is None:
            return {}
        return {"ttl": self._ttl}

    def _handle_missing_record(self, exc: BaseException) -> bool:
        """Return True if ``exc`` is Aerospike's record-not-found error."""
        # RecordNotFound is canonical; older clients may raise plain
        # AerospikeError with code 2. Handle both without hard-depending on
        # the submodule at import time.
        exc_module = getattr(aerospike, "exception", None)
        record_not_found = getattr(exc_module, "RecordNotFound", None) if exc_module else None
        if record_not_found is not None and isinstance(exc, record_not_found):
            return True
        code = getattr(exc, "code", None)
        return code == 2  # AEROSPIKE_ERR_RECORD_NOT_FOUND

    async def _get_next_id(self) -> int:
        """Atomically increment and return the session's per-item counter.

        Not used internally by :meth:`add_items`, since the ordered-list bin
        already preserves insertion order on the server. Exposed for tests
        and for consumers that want a monotonic per-session sequence number.
        """

        def _op() -> int:
            _, _, bins = self._client.operate(
                self._record_key,
                [ops.increment(_BIN_COUNTER, 1), ops.read(_BIN_COUNTER)],
                meta=self._write_meta(),
            )
            value = bins.get(_BIN_COUNTER)
            return int(value) if value is not None else 0

        return await asyncio.to_thread(_op)

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If ``None``, falls
                back to ``session_settings.limit``; if that is also ``None``,
                returns all items.

        Returns:
            A list of input items in chronological (oldest-first) order.
        """
        session_limit = resolve_session_limit(limit, self.session_settings)
        if session_limit is not None and session_limit <= 0:
            return []

        async with self._lock:
            raw_messages = await asyncio.to_thread(self._get_items_sync, session_limit)

        items: list[TResponseInputItem] = []
        for raw in raw_messages:
            try:
                if isinstance(raw, bytes):
                    raw_str = raw.decode("utf-8")
                elif isinstance(raw, str):
                    raw_str = raw
                else:
                    # Unexpected bin value shape (e.g. already-decoded dict).
                    # Accept dicts as a best-effort passthrough.
                    if isinstance(raw, dict):
                        items.append(raw)  # type: ignore[arg-type]
                    continue
                items.append(await self._deserialize_item(raw_str))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return items

    def _get_items_sync(self, session_limit: int | None) -> list[Any]:
        """Blocking half of :meth:`get_items`."""
        try:
            if session_limit is None:
                op_list = [list_ops.list_get_range(_BIN_MESSAGES, 0, 2**31 - 1)]
            else:
                # list_get_range with a negative index counts from the end.
                # We want the last N items in chronological order, which is
                # exactly what Aerospike returns for (index=-N, count=N).
                op_list = [list_ops.list_get_range(_BIN_MESSAGES, -session_limit, session_limit)]
            _, _, bins = self._client.operate(self._record_key, op_list)
        except Exception as exc:  # noqa: BLE001 - we re-classify below
            if self._handle_missing_record(exc):
                return []
            raise
        raw = bins.get(_BIN_MESSAGES)
        if raw is None:
            return []
        return list(raw)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Append items to the conversation history.

        The entire operation is one server-side multi-op: metadata bins,
        ordered-list append, and atomic counter increment are applied as a
        single atomic unit on the record.
        """
        if not items:
            return

        serialized: list[str] = []
        for item in items:
            serialized.append(await self._serialize_item(item))

        now = int(time.time())

        async with self._lock:
            await asyncio.to_thread(self._add_items_sync, serialized, now)

    def _add_items_sync(self, serialized: list[str], now: int) -> None:
        """Blocking half of :meth:`add_items`."""
        op_list = [
            # Upsert session metadata.
            ops.write(_BIN_SESSION_ID, self.session_id),
            ops.write(_BIN_UPDATED_AT, now),
            # created_at is upserted on every write for simplicity. Consumers
            # who need the true creation time should capture it themselves
            # at session construction; a future version may switch to an
            # "insert-only" bin policy if there is demand.
            ops.write(_BIN_CREATED_AT, now),
            # Atomic counter bump.
            ops.increment(_BIN_COUNTER, len(serialized)),
            # Ordered-list append preserves insertion order.
            list_ops.list_append_items(_BIN_MESSAGES, serialized),
        ]
        try:
            self._client.operate(self._record_key, op_list, meta=self._write_meta())
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if _is_record_too_big(exc):
                raise SessionRecordTooLargeError(
                    self.session_id,
                    attempted_payload_bytes=sum(len(s) for s in serialized),
                    cause=exc,
                ) from exc
            raise

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns ``None`` if the session is empty or the record does not exist.
        """
        async with self._lock:
            raw = await asyncio.to_thread(self._pop_item_sync)

        if raw is None:
            return None
        try:
            if isinstance(raw, bytes):
                return await self._deserialize_item(raw.decode("utf-8"))
            if isinstance(raw, str):
                return await self._deserialize_item(raw)
            if isinstance(raw, dict):
                return raw  # type: ignore[return-value]
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return None

    def _pop_item_sync(self) -> Any:
        """Blocking half of :meth:`pop_item`."""
        try:
            # list_pop at index -1 atomically removes and returns the tail item.
            _, _, bins = self._client.operate(
                self._record_key,
                [list_ops.list_pop(_BIN_MESSAGES, -1)],
                meta=self._write_meta(),
            )
        except Exception as exc:  # noqa: BLE001
            if self._handle_missing_record(exc):
                return None
            # Aerospike raises OpNotApplicable when the list is empty or the
            # bin is missing. Treat that as "nothing to pop".
            exc_module = getattr(aerospike, "exception", None)
            op_not_applicable = getattr(exc_module, "OpNotApplicable", None) if exc_module else None
            if op_not_applicable is not None and isinstance(exc, op_not_applicable):
                return None
            raise
        return bins.get(_BIN_MESSAGES)

    async def clear_session(self) -> None:
        """Remove the session record entirely."""

        def _op() -> None:
            try:
                self._client.remove(self._record_key)
            except Exception as exc:  # noqa: BLE001
                if self._handle_missing_record(exc):
                    return
                raise

        async with self._lock:
            await asyncio.to_thread(_op)

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the Aerospike client if this session owns it.

        Sessions constructed via :meth:`from_hosts` own their client and will
        close it here. Sessions passed an externally-managed client leave it
        untouched; the caller remains responsible for the client's lifecycle.
        """
        if self._owns_client:
            await asyncio.to_thread(self._client.close)

    async def ping(self) -> bool:
        """Test connectivity to the Aerospike cluster.

        Returns:
            ``True`` if the cluster is reachable, ``False`` otherwise.
        """

        def _op() -> bool:
            try:
                # is_connected() is cheap and does not hit the network on a
                # healthy client; a nodes() call confirms cluster visibility.
                if not self._client.is_connected():
                    return False
                nodes = self._client.get_nodes()
                return bool(nodes)
            except Exception:  # noqa: BLE001
                return False

        return await asyncio.to_thread(_op)
