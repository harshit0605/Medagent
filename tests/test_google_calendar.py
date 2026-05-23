"""Unit tests for the Google Calendar client primitives.

httpx + the OAuth refresh + the calendar API are mocked. The tests cover:
- access-token refresh path (cached vs expired)
- freeBusy parsing + slot carving
- event create body shape
- 401 retry-then-fail flow

DB writes are exercised via a tiny in-memory monkeypatch on the doctors repo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _scoped_fernet_key(monkeypatch):
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    import app.security.crypto as crypto_module

    crypto_module._fernet = None
    yield
    crypto_module._fernet = None


# ---- A pretend Doctor + repo ------------------------------------------------


class _FakeDoctor:
    def __init__(self, id: int):
        from app.db.models import DoctorOAuthStatus
        from app.security.crypto import encrypt

        self.id = id
        self.calendar_id = "primary"
        self.timezone = "UTC"
        self.oauth_status = DoctorOAuthStatus.connected
        self.oauth_refresh_token_enc = encrypt("test-refresh-token")
        self.oauth_access_token = "cached-access"
        self.oauth_access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)


@pytest.fixture
def patch_doctors_repo(monkeypatch):
    from services.orchestrator import google_calendar as gcal_module

    captured: dict[str, Any] = {"updates": []}

    fake_doctor = _FakeDoctor(id=42)

    async def fake_get(_session, doctor_id):  # noqa: ARG001
        return fake_doctor if doctor_id == 42 else None

    async def fake_update(session, doctor_id, *, access_token, access_token_expires_at):  # noqa: ARG001
        captured["updates"].append(
            {
                "doctor_id": doctor_id,
                "access_token": access_token,
                "expires_at": access_token_expires_at,
            }
        )
        fake_doctor.oauth_access_token = access_token
        fake_doctor.oauth_access_token_expires_at = access_token_expires_at
        return fake_doctor

    async def fake_mark_disconnected(session, doctor_id, *, status):  # noqa: ARG001
        from app.db.models import DoctorOAuthStatus

        captured["disconnect_status"] = status.value
        fake_doctor.oauth_status = DoctorOAuthStatus.disconnected
        return fake_doctor

    monkeypatch.setattr(gcal_module.doctors_repo, "get", fake_get)
    monkeypatch.setattr(gcal_module.doctors_repo, "update_access_token", fake_update)
    monkeypatch.setattr(gcal_module.doctors_repo, "mark_disconnected", fake_mark_disconnected)

    return {"doctor": fake_doctor, "captured": captured}


# ---- httpx.AsyncClient mocks -----------------------------------------------


class _MockResponse:
    def __init__(self, payload: Any | None = None, status_code: int = 200, content: bool = True):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if content else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _MockAsyncClient:
    """Configurable stand-in for httpx.AsyncClient.

    `handlers` is a list of callables (method, url, **kwargs) -> _MockResponse,
    consumed in order — one per HTTP call inside the tested function.
    """

    def __init__(self, *, handlers, captured):
        # Share the list reference across clients — each `httpx.AsyncClient(...)`
        # gets a NEW _MockAsyncClient but they all consume from the same queue
        # so call ordering across multiple httpx.AsyncClient() blocks is deterministic.
        self._handlers = handlers
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def post(self, url, *, params=None, json=None, headers=None, data=None):
        return self._dispatch("POST", url, params=params, json=json, headers=headers, data=data)

    async def get(self, url, *, params=None, headers=None):
        return self._dispatch("GET", url, params=params, headers=headers)

    async def request(self, method, url, *, params=None, json=None, headers=None):
        return self._dispatch(method, url, params=params, json=json, headers=headers)

    def _dispatch(self, method, url, **kwargs):
        if not self._handlers:
            raise AssertionError(f"unexpected extra HTTP call: {method} {url}")
        handler = self._handlers.pop(0)
        self._captured.append({"method": method, "url": url, **kwargs})
        return handler(method=method, url=url, **kwargs)


def _patch_async_client(monkeypatch, handlers, captured=None):
    if captured is None:
        captured = []
    from services.orchestrator import google_calendar as gcal_module

    def factory(**_kwargs):
        return _MockAsyncClient(handlers=handlers, captured=captured)

    monkeypatch.setattr(gcal_module.httpx, "AsyncClient", factory)
    return captured


# ---- Tests ------------------------------------------------------------------


async def test_find_slots_carves_free_blocks(monkeypatch, patch_doctors_repo):
    from services.orchestrator import google_calendar as gcal

    handlers = [
        # /freeBusy
        lambda **_kw: _MockResponse(
            payload={
                "calendars": {
                    "primary": {
                        "busy": [
                            {"start": "2026-05-01T10:00:00Z", "end": "2026-05-01T10:30:00Z"},
                        ]
                    }
                }
            }
        ),
    ]
    captured = _patch_async_client(monkeypatch, handlers)

    window = gcal.TimeSlot(
        start=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc),
    )
    result = await gcal.find_slots(None, doctor_id=42, window=window, duration_minutes=30)
    assert len(result.busy) == 1
    # Window 9–11 minus busy 10:00–10:30 with 30-min slots → 9:00, 9:30, 10:30
    starts = [s.start for s in result.free]
    assert starts == [
        datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 1, 10, 30, tzinfo=timezone.utc),
    ]
    # The freeBusy POST went to the right path with the doctor's calendar id.
    call = captured[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/freeBusy")
    assert call["json"]["items"] == [{"id": "primary"}]


async def test_book_slot_builds_event_with_attendees_and_extended_properties(
    monkeypatch, patch_doctors_repo
):
    from services.orchestrator import google_calendar as gcal

    created_event = {
        "id": "evt-123",
        "summary": "Consultation",
        "start": {"dateTime": "2026-05-02T09:00:00Z"},
        "end": {"dateTime": "2026-05-02T09:30:00Z"},
        "htmlLink": "https://calendar.google.com/event?eid=evt-123",
        "attendees": [{"email": "patient@example.com"}],
        "status": "confirmed",
    }
    handlers = [lambda **_kw: _MockResponse(payload=created_event)]
    captured = _patch_async_client(monkeypatch, handlers)

    event = await gcal.book_slot(
        None,
        doctor_id=42,
        start=datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, 9, 30, tzinfo=timezone.utc),
        summary="Consultation",
        patient_email="patient@example.com",
        patient_phone="918340858764",
    )
    assert event.event_id == "evt-123"

    body = captured[0]["json"]
    assert body["summary"] == "Consultation"
    assert body["start"]["timeZone"] == "UTC"
    emails = [a["email"] for a in body["attendees"]]
    assert "patient@example.com" in emails
    assert body["extendedProperties"]["private"]["medagent_patient_phone"] == "918340858764"
    # sendUpdates=all so attendees actually get the invite
    assert captured[0]["params"] == {"sendUpdates": "all"}


async def test_token_refresh_fires_when_cache_is_expired(monkeypatch, patch_doctors_repo):
    from services.orchestrator import google_calendar as gcal

    fake = patch_doctors_repo["doctor"]
    fake.oauth_access_token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    handlers = [
        # token refresh
        lambda **_kw: _MockResponse(payload={"access_token": "fresh", "expires_in": 3600}),
        # actual freeBusy
        lambda **_kw: _MockResponse(payload={"calendars": {"primary": {"busy": []}}}),
    ]
    captured = _patch_async_client(monkeypatch, handlers)

    window = gcal.TimeSlot(
        start=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
    )
    await gcal.find_slots(None, doctor_id=42, window=window, duration_minutes=30)

    # First call hit the token endpoint; second hit calendar API with the new token.
    assert captured[0]["url"] == gcal.GOOGLE_TOKEN_URL
    assert captured[1]["url"].endswith("/freeBusy")
    assert captured[1]["headers"]["Authorization"] == "Bearer fresh"
    assert patch_doctors_repo["captured"]["updates"][0]["access_token"] == "fresh"


async def test_401_after_refresh_marks_doctor_expired(monkeypatch, patch_doctors_repo):
    from services.orchestrator import google_calendar as gcal

    handlers = [
        lambda **_kw: _MockResponse(payload=None, status_code=401),  # first calendar call
        lambda **_kw: _MockResponse(  # token refresh
            payload={"access_token": "fresh", "expires_in": 3600}
        ),
        lambda **_kw: _MockResponse(payload=None, status_code=401),  # retry, still 401
    ]
    _patch_async_client(monkeypatch, handlers)

    window = gcal.TimeSlot(
        start=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(PermissionError):
        await gcal.find_slots(None, doctor_id=42, window=window, duration_minutes=30)
    assert patch_doctors_repo["captured"]["disconnect_status"] == "expired"
