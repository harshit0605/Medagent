"""Unit tests for the Meta Cloud API client (no real HTTP).

Verifies the request body shape we ship to ``graph.facebook.com``, the
dry-run short-circuit, and the failure surface.
"""

from __future__ import annotations

import httpx
import pytest

from services.whatsapp_gateway import meta as meta_module


@pytest.fixture(autouse=True)
def _force_real_mode(monkeypatch):
    """Each test should opt-in to dry-run; default here is "real" with no token
    so accidental network calls fail loudly."""
    monkeypatch.setenv("WHATSAPP_DRY_RUN", "0")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "111222333")
    monkeypatch.setenv("WHATSAPP_GRAPH_VERSION", "v22.0")
    monkeypatch.setenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")
    yield


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """No-op the retry backoff so retry tests run instantly."""

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(meta_module.asyncio, "sleep", _no_sleep)
    yield


class _MockResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload or {"messages": [{"id": "wamid.AAA"}]}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", "x"), response=httpx.Response(self.status_code)
            )

    def json(self) -> dict:
        return self._payload


class _MockAsyncClient:
    def __init__(self, *, post=None, captured: dict | None = None) -> None:
        self._post = post or (lambda url, json, headers: _MockResponse())
        self._captured = captured if captured is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a) -> None:
        return None

    async def post(self, url, json, headers):
        self._captured["url"] = url
        self._captured["json"] = json
        self._captured["headers"] = headers
        return self._post(url, json, headers)


def _patch_async_client(monkeypatch, post=None) -> dict:
    captured: dict = {}

    def factory(*_a, **_k):
        return _MockAsyncClient(post=post, captured=captured)

    monkeypatch.setattr(meta_module.httpx, "AsyncClient", factory)
    return captured


async def test_freeform_builds_correct_body_and_extracts_wamid(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    result = await meta_module.send_freeform(to="+16315551234", text="hello")
    assert result.accepted is True
    assert result.dry_run is False
    assert result.payload_kind == "freeform"
    assert result.wamid == "wamid.AAA"
    assert captured["url"] == "https://graph.facebook.com/v22.0/111222333/messages"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "16315551234",  # leading + stripped
        "type": "text",
        "text": {"body": "hello"},
    }


async def test_template_builds_components_with_sorted_body_params(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    result = await meta_module.send_template(
        to="16315551234",
        template_name="dose_reminder_v1",
        template_params={"medication_name": "metformin", "dose": "500mg"},
    )
    assert result.accepted is True
    assert result.payload_kind == "template"
    body = captured["json"]
    assert body["type"] == "template"
    assert body["template"]["name"] == "dose_reminder_v1"
    assert body["template"]["language"] == {"code": "en"}
    body_params = body["template"]["components"][0]["parameters"]
    # Sorted by key for determinism: dose < medication_name
    assert body_params == [
        {"type": "text", "text": "500mg"},
        {"type": "text", "text": "metformin"},
    ]


async def test_template_omits_components_when_no_params(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    await meta_module.send_template(
        to="16315551234", template_name="hello_world_v1"
    )
    body = captured["json"]
    assert "components" not in body["template"]


async def test_template_uses_explicit_language_over_env(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    await meta_module.send_template(
        to="16315551234", template_name="hello", language="fr"
    )
    assert captured["json"]["template"]["language"] == {"code": "fr"}


async def test_dry_run_skips_http(monkeypatch):
    monkeypatch.setenv("WHATSAPP_DRY_RUN", "1")

    def must_not_call(*_a, **_k):
        raise AssertionError("dry run should not hit HTTP")

    monkeypatch.setattr(meta_module.httpx, "AsyncClient", lambda **_k: must_not_call())

    result = await meta_module.send_freeform(to="123", text="hi")
    assert result.accepted is True
    assert result.dry_run is True
    assert result.wamid is None


async def test_dry_run_default_when_no_token(monkeypatch):
    monkeypatch.delenv("WHATSAPP_DRY_RUN", raising=False)
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)

    result = await meta_module.send_freeform(to="123", text="hi")
    assert result.dry_run is True
    assert result.accepted is True


async def test_http_error_returns_failed_result_with_error_string(monkeypatch):
    def boom(url, json, headers):  # noqa: ARG001
        raise httpx.ConnectError("refused")

    _patch_async_client(monkeypatch, post=boom)
    result = await meta_module.send_freeform(to="123", text="hi")
    assert result.accepted is False
    assert result.dry_run is False
    assert result.error and result.error.startswith("http_error:")


async def test_normalize_to_strips_leading_plus():
    assert meta_module._normalize_to("+16315551234") == "16315551234"
    assert meta_module._normalize_to("16315551234") == "16315551234"
    assert meta_module._normalize_to(" +16315551234 ") == "16315551234"


# ---- transient-failure retry/backoff --------------------------------------


async def test_transient_5xx_is_retried_then_succeeds(monkeypatch):
    """A 5xx is transient — _post_to_meta retries and succeeds on a later
    attempt instead of surfacing the blip as a failed send."""
    calls = {"n": 0}

    def post(url, json, headers):  # noqa: A002
        calls["n"] += 1
        if calls["n"] < 3:
            return _MockResponse(status_code=503)
        return _MockResponse()  # success on the 3rd attempt

    _patch_async_client(monkeypatch, post=post)
    result = await meta_module.send_freeform(to="15551234567", text="hi")
    assert result.accepted is True
    assert calls["n"] == 3


async def test_permanent_4xx_is_not_retried(monkeypatch):
    """A 4xx is permanent — retrying won't help, so it fails on the first
    attempt without burning extra calls."""
    calls = {"n": 0}

    def post(url, json, headers):  # noqa: A002
        calls["n"] += 1
        return _MockResponse(status_code=400)

    _patch_async_client(monkeypatch, post=post)
    result = await meta_module.send_freeform(to="15551234567", text="hi")
    assert result.accepted is False
    assert calls["n"] == 1


async def test_transient_failure_exhausts_attempts(monkeypatch):
    """A sustained transient failure exhausts the bounded attempts and then
    surfaces as a failed send (the scheduler's retry/DLQ takes over)."""
    calls = {"n": 0}

    def post(url, json, headers):  # noqa: A002
        calls["n"] += 1
        return _MockResponse(status_code=503)

    _patch_async_client(monkeypatch, post=post)
    result = await meta_module.send_freeform(to="15551234567", text="hi")
    assert result.accepted is False
    assert calls["n"] == meta_module._MAX_SEND_ATTEMPTS
