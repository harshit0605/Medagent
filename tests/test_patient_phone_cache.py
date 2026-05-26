"""Session-scoped memoization for ``patients_repo.get_by_phone``.

The cache lives on ``AsyncSession.info`` so it shares the session's lifetime
— no manual TTL, no cross-session ORM-identity pitfalls. Verified here in
isolation with a mock session; integration tests cover the real-DB path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.repositories import patients as patients_repo


class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Minimal AsyncSession stub. Tracks how many times ``execute`` ran so
    we can assert the memo skips the query on cache hit."""

    def __init__(self, *, return_value: Any) -> None:
        self.info: dict[str, Any] = {}
        self.execute_calls = 0
        self._return = return_value

    async def execute(self, _stmt: Any) -> _FakeScalarResult:
        self.execute_calls += 1
        return _FakeScalarResult(self._return)


@pytest.mark.asyncio
async def test_first_call_hits_db_second_call_uses_cache():
    sentinel = object()
    session = _FakeSession(return_value=sentinel)

    first = await patients_repo.get_by_phone(session, "+919999911111")  # type: ignore[arg-type]
    second = await patients_repo.get_by_phone(session, "+919999911111")  # type: ignore[arg-type]

    assert first is sentinel
    assert second is sentinel
    assert session.execute_calls == 1, "second call should be served from session cache"


@pytest.mark.asyncio
async def test_different_phones_each_hit_db_once():
    session = _FakeSession(return_value=None)

    await patients_repo.get_by_phone(session, "+919999911111")  # type: ignore[arg-type]
    await patients_repo.get_by_phone(session, "+919999922222")  # type: ignore[arg-type]
    await patients_repo.get_by_phone(session, "+919999911111")  # type: ignore[arg-type]
    await patients_repo.get_by_phone(session, "+919999922222")  # type: ignore[arg-type]

    assert session.execute_calls == 2, "each distinct phone is queried once per session"


@pytest.mark.asyncio
async def test_negative_result_is_cached_too():
    """A patient who doesn't exist shouldn't be re-queried on every miss.
    The cache stores ``None`` and short-circuits future lookups."""
    session = _FakeSession(return_value=None)

    first = await patients_repo.get_by_phone(session, "+919999900000")  # type: ignore[arg-type]
    second = await patients_repo.get_by_phone(session, "+919999900000")  # type: ignore[arg-type]

    assert first is None
    assert second is None
    assert session.execute_calls == 1, "negative cache hit avoids the second query"


@pytest.mark.asyncio
async def test_invalidate_phone_forces_refetch():
    session = _FakeSession(return_value=None)

    await patients_repo.get_by_phone(session, "+919999933333")  # type: ignore[arg-type]
    patients_repo._invalidate_phone(session, "+919999933333")  # type: ignore[arg-type]
    await patients_repo.get_by_phone(session, "+919999933333")  # type: ignore[arg-type]

    assert session.execute_calls == 2, "invalidation forces the next call to re-query"


@pytest.mark.asyncio
async def test_separate_sessions_have_independent_caches():
    """Each ``AsyncSession.info`` is its own dict — two sessions don't share
    cache entries (which would create cross-session ORM-identity bugs)."""
    sentinel_a = object()
    sentinel_b = object()
    session_a = _FakeSession(return_value=sentinel_a)
    session_b = _FakeSession(return_value=sentinel_b)

    result_a = await patients_repo.get_by_phone(session_a, "+919999944444")  # type: ignore[arg-type]
    result_b = await patients_repo.get_by_phone(session_b, "+919999944444")  # type: ignore[arg-type]

    assert result_a is sentinel_a
    assert result_b is sentinel_b
    assert session_a.execute_calls == 1
    assert session_b.execute_calls == 1
