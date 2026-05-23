"""Integration tests for the delivery-metrics rollup.

The rollup joins ``message_log`` (outbound rows, since X) to
``whatsapp_message_statuses`` (status events keyed by wamid). Tested
against a real Postgres because the join + the bucket CASE expression
exercise SQL behaviour that mocks would obscure.

Skipped when DATABASE_URL is unset so CI without a Postgres still
passes.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.repositories import (
    delivery_metrics as delivery_metrics_repo,
    message_log as message_log_repo,
    whatsapp_statuses as whatsapp_statuses_repo,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping delivery_metrics integration tests",
)


def _patient() -> str:
    return f"itest-deliv-{uuid.uuid4().hex[:10]}"


def _wamid() -> str:
    return f"wamid.{uuid.uuid4().hex}"


async def _seed_outbound(
    *,
    patient: str,
    payload_kind: str = "freeform",
    wamid: str | None = None,
    extra_payload: dict | None = None,
    occurred_at: datetime | None = None,
) -> None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await message_log_repo.append_outbound(
            db,
            patient_id=patient,
            payload_kind=payload_kind,
            payload=extra_payload or {},
            occurred_at=occurred_at or datetime.now(timezone.utc),
            wamid=wamid,
        )
        await db.commit()


async def _seed_status(
    *,
    wamid: str,
    status: str,
    error_code: int | None = None,
    error_title: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await whatsapp_statuses_repo.upsert(
            db,
            wamid=wamid,
            status=status,
            recipient_id="91+test",
            timestamp=timestamp or datetime.now(timezone.utc),
            error_code=error_code,
            error_title=error_title,
            raw={},
        )
        await db.commit()


async def test_append_outbound_persists_wamid_as_first_class_column():
    """``append_outbound(wamid=...)`` writes to the new column, not
    just the JSON payload — that's what makes the delivery join
    cheap. Without this, the partial index can't be used and per-
    template metrics would need a JSON path scan."""
    patient = _patient()
    wamid = _wamid()
    await _seed_outbound(patient=patient, wamid=wamid)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await message_log_repo.recent(db, limit=50)
        match = next((r for r in rows if r.patient_id == patient), None)
        assert match is not None
        assert match.wamid == wamid


async def test_delivery_summary_counts_each_status_bucket():
    """Seed one outbound + status pair for each terminal bucket and
    confirm the rollup picks them up exactly."""
    patient = _patient()
    since = datetime.now(timezone.utc) - timedelta(minutes=5)

    cases = [
        ("delivered", _wamid(), "delivered", None, None),
        ("read", _wamid(), "read", None, None),
        ("sent_only", _wamid(), "sent", None, None),
        ("failed", _wamid(), "failed", 131047, "Re-engagement"),
    ]
    for _bucket, wamid, status, code, title in cases:
        await _seed_outbound(patient=patient, wamid=wamid)
        await _seed_status(
            wamid=wamid, status=status, error_code=code, error_title=title
        )

    # Pre-Meta failure: outbound row exists but no wamid AND _send_error
    # is set. Should classify as failed_pre_meta.
    await _seed_outbound(
        patient=patient,
        wamid=None,
        extra_payload={"_send_error": "meta 502"},
    )

    # Edge case: wamid present but no status row. Should classify
    # as no_status_yet (waiting on webhook).
    await _seed_outbound(patient=patient, wamid=_wamid())

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        summary = await delivery_metrics_repo.delivery_summary(
            db, since=since
        )

    # 6 seeded rows for this patient — the global query may include
    # other test rows, so we assert this patient's contributions are
    # at minimum reflected in the totals.
    assert summary["total_outbound"] >= 6
    bs = summary["by_status"]
    assert bs["delivered"] >= 1
    assert bs["read"] >= 1
    assert bs["sent_only"] >= 1
    assert bs["failed"] >= 1
    assert bs["failed_pre_meta"] >= 1
    assert bs["no_status_yet"] >= 1
    # Top failure codes should include the 131047 we seeded.
    codes = {f["code"] for f in summary["top_failure_codes"]}
    assert 131047 in codes


async def test_delivery_summary_groups_by_payload_kind():
    """Per-payload-kind breakdown lets ops see whether template sends
    are failing more than freeform replies — the most common
    delivery-rate divergence in practice."""
    patient = _patient()
    since = datetime.now(timezone.utc) - timedelta(minutes=5)

    # 3 templates: 2 delivered, 1 failed.
    for _ in range(2):
        w = _wamid()
        await _seed_outbound(patient=patient, payload_kind="template", wamid=w)
        await _seed_status(wamid=w, status="delivered")
    w_fail = _wamid()
    await _seed_outbound(patient=patient, payload_kind="template", wamid=w_fail)
    await _seed_status(
        wamid=w_fail, status="failed", error_code=131047, error_title="Re-engagement"
    )

    # 1 freeform: delivered.
    w_free = _wamid()
    await _seed_outbound(patient=patient, payload_kind="freeform", wamid=w_free)
    await _seed_status(wamid=w_free, status="delivered")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        summary = await delivery_metrics_repo.delivery_summary(
            db, since=since
        )

    tpl = summary["by_payload_kind"]["template"]
    assert tpl["total"] >= 3
    assert tpl["delivered"] >= 2
    assert tpl["failed"] >= 1

    free = summary["by_payload_kind"]["freeform"]
    assert free["total"] >= 1
    assert free["delivered"] >= 1


async def test_delivery_summary_excludes_rows_before_since():
    """``since`` is the lower bound — rows from before that timestamp
    must NOT appear in the rollup. Without this, the dashboard's
    "last 24h" window would include all-time totals."""
    patient = _patient()
    long_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)

    # Old delivered message — should be excluded from a 5-minute window.
    w_old = _wamid()
    await _seed_outbound(
        patient=patient, wamid=w_old, occurred_at=long_ago
    )
    await _seed_status(wamid=w_old, status="delivered", timestamp=long_ago)

    # Recent delivered message — should be included.
    w_new = _wamid()
    await _seed_outbound(
        patient=patient, wamid=w_new, occurred_at=recent
    )
    await _seed_status(wamid=w_new, status="delivered")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        summary_recent = await delivery_metrics_repo.delivery_summary(
            db, since=datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        summary_all = await delivery_metrics_repo.delivery_summary(
            db, since=datetime.now(timezone.utc) - timedelta(days=14)
        )

    # The 7-day window must include MORE outbound rows for this patient
    # than the 5-min window — the old row is in the 7-day but not 5-min.
    # We can't assert exact counts because tests share the DB with
    # other rows, so use the differential.
    assert summary_all["total_outbound"] > summary_recent["total_outbound"]


async def test_delivery_summary_zero_outbound_returns_zero_rates():
    """Empty window → no division-by-zero, all-zero shape."""
    SessionLocal = get_sessionmaker()
    far_future = datetime.now(timezone.utc) + timedelta(days=1)
    async with SessionLocal() as db:
        summary = await delivery_metrics_repo.delivery_summary(
            db, since=far_future
        )
    assert summary["total_outbound"] == 0
    assert summary["delivery_rate"] == 0.0
    assert summary["failure_rate"] == 0.0
    assert summary["top_failure_codes"] == []


def test_dashboard_endpoint_includes_delivery_block():
    """End-to-end: GET /ops/dashboard returns a top-level
    ``delivery`` object with the expected shape so the ops-console
    UI can render the tile without any orchestrator changes."""
    from fastapi.testclient import TestClient

    from services.orchestrator.main import app

    with TestClient(app) as client:
        response = client.get("/ops/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert "delivery" in body
    delivery = body["delivery"]
    assert "total_outbound" in delivery
    assert "by_status" in delivery
    for bucket in (
        "delivered",
        "read",
        "sent_only",
        "failed",
        "failed_pre_meta",
        "no_status_yet",
    ):
        assert bucket in delivery["by_status"]
    assert "delivery_rate" in delivery
    assert "failure_rate" in delivery
    assert "by_payload_kind" in delivery
    assert "top_failure_codes" in delivery


# ---- Per-template breakdown ---------------------------------------------


def _unique_template(prefix: str) -> str:
    """Per-test template name suffix. The integration suite has no
    per-test isolation — rows persist across runs — so reusing real
    template names like ``dose_reminder_v1`` would let prior runs'
    rows poison the assertions. A uuid suffix gives each test its
    own template namespace."""
    return f"{prefix}__test_{uuid.uuid4().hex[:8]}"


async def test_summary_by_template_groups_by_template_name():
    """Two templates with different delivery profiles must roll up
    independently — that's the whole point of the per-template
    breakdown. A "v1 healthy + v2 silently failing" mix is exactly
    the failure mode this catches."""
    patient = _patient()
    healthy = _unique_template("dose_reminder_v1")
    failing = _unique_template("dose_reminder_v2")
    # 2 healthy v1 sends.
    for _ in range(2):
        wamid = _wamid()
        await _seed_outbound(
            patient=patient,
            payload_kind="template",
            wamid=wamid,
            extra_payload={"template_name": healthy},
        )
        await _seed_status(wamid=wamid, status="delivered")
    # 1 v2 failed at Meta.
    failing_wamid = _wamid()
    await _seed_outbound(
        patient=patient,
        payload_kind="template",
        wamid=failing_wamid,
        extra_payload={"template_name": failing},
    )
    await _seed_status(
        wamid=failing_wamid,
        status="failed",
        error_code=131000,
        error_title="Generic error",
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await delivery_metrics_repo.delivery_summary_by_template(
            db, since=datetime.now(timezone.utc) - timedelta(hours=1)
        )

    by_name = {r["template_name"]: r for r in rows}
    assert by_name[healthy]["total"] == 2
    assert by_name[healthy]["delivered"] == 2
    assert by_name[healthy]["failed"] == 0
    assert by_name[healthy]["delivery_rate"] == 1.0
    assert by_name[healthy]["failure_rate"] == 0.0
    assert by_name[failing]["total"] == 1
    assert by_name[failing]["failed"] == 1
    assert by_name[failing]["delivered"] == 0
    assert by_name[failing]["failure_rate"] == 1.0


async def test_summary_by_template_excludes_freeform_sends():
    """Freeform sends don't have a template_name and would
    aggregate under ``<unknown>`` if they leaked into this rollup.
    The query filters to ``payload_kind = template`` so they
    don't appear at all."""
    patient = _patient()
    template = _unique_template("appointment_reminder_v1")
    # Template send.
    wamid_t = _wamid()
    await _seed_outbound(
        patient=patient,
        payload_kind="template",
        wamid=wamid_t,
        extra_payload={"template_name": template},
    )
    await _seed_status(wamid=wamid_t, status="delivered")
    # Freeform send — must NOT show up.
    wamid_f = _wamid()
    await _seed_outbound(
        patient=patient,
        payload_kind="freeform",
        wamid=wamid_f,
        extra_payload={"body": "freeform reply"},
    )
    await _seed_status(wamid=wamid_f, status="delivered")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await delivery_metrics_repo.delivery_summary_by_template(
            db, since=datetime.now(timezone.utc) - timedelta(hours=1)
        )

    by_name = {r["template_name"]: r for r in rows}
    assert template in by_name
    assert by_name[template]["total"] == 1
    # The freeform send must NOT have leaked into the rollup. We
    # can't easily check by patient (no patient column on the
    # rollup), but the unique template name guarantees the only
    # row we OUR test created with this prefix is the template send.


async def test_summary_by_template_sorts_by_volume_desc():
    """Busiest templates lead so ops can scan top-down. A 1000-send
    template at 99% delivery is more interesting than a 5-send
    template at 80% — sort surfaces the high-volume ones first."""
    patient = _patient()
    low = _unique_template("lab_closure_update_v1")
    high = _unique_template("appointment_reminder_v1")
    # Lower-volume template.
    for _ in range(2):
        wamid = _wamid()
        await _seed_outbound(
            patient=patient,
            payload_kind="template",
            wamid=wamid,
            extra_payload={"template_name": low},
        )
        await _seed_status(wamid=wamid, status="delivered")
    # Higher-volume template.
    for _ in range(5):
        wamid = _wamid()
        await _seed_outbound(
            patient=patient,
            payload_kind="template",
            wamid=wamid,
            extra_payload={"template_name": high},
        )
        await _seed_status(wamid=wamid, status="delivered")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await delivery_metrics_repo.delivery_summary_by_template(
            db, since=datetime.now(timezone.utc) - timedelta(hours=1)
        )

    # Filter to just our seeded names so the test isn't sensitive
    # to other rows the test session might have left around.
    ours = [r for r in rows if r["template_name"] in (low, high)]
    assert len(ours) == 2
    # high (5 sends) must come before low (2 sends) in the sorted
    # output.
    assert ours[0]["template_name"] == high
    assert ours[1]["template_name"] == low


async def test_summary_by_template_counts_pre_meta_failures():
    """Pre-Meta failures (no wamid + ``_send_error`` set) are
    operationally distinct from Meta-side failures — they mean OUR
    pipeline broke. The breakdown surfaces them as a separate
    field so ops can spot pipeline issues without conflating them
    with recipient-side failures."""
    patient = _patient()
    template = _unique_template("post_visit_recap_v1")
    # One pre-Meta failure: outbound row with no wamid + _send_error.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await message_log_repo.append_outbound(
            db,
            patient_id=patient,
            payload_kind="template",
            payload={
                "template_name": template,
                "_send_error": "auth_token_expired",
            },
            occurred_at=datetime.now(timezone.utc),
            wamid=None,
        )
        await db.commit()

    async with SessionLocal() as db:
        rows = await delivery_metrics_repo.delivery_summary_by_template(
            db, since=datetime.now(timezone.utc) - timedelta(hours=1)
        )
    by_name = {r["template_name"]: r for r in rows}
    assert by_name[template]["failed_pre_meta"] == 1
    assert by_name[template]["failed"] == 0
    assert by_name[template]["failure_rate"] == 1.0


def test_dashboard_endpoint_includes_delivery_by_template():
    """End-to-end: GET /ops/dashboard exposes ``delivery_by_template``
    so the ops-console UI can render the per-template breakdown
    table without a separate fetch."""
    from fastapi.testclient import TestClient

    from services.orchestrator.main import app

    with TestClient(app) as client:
        response = client.get("/ops/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert "delivery_by_template" in body
    # Always a list (possibly empty in clean envs).
    assert isinstance(body["delivery_by_template"], list)
    # Spot-check shape on any row that's present.
    for row in body["delivery_by_template"]:
        for field in (
            "template_name",
            "total",
            "delivered",
            "failed",
            "failed_pre_meta",
            "delivery_rate",
            "failure_rate",
        ):
            assert field in row
