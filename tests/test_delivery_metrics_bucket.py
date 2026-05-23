"""Unit tests for the ``_bucket_of`` Python-side helper.

The helper exists to mirror the SQL ``CASE`` expression used in the
``delivery_summary`` rollup. Both must agree on how (status, has_wamid,
has_send_error) maps to a bucket, so we keep the Python implementation
testable without a DB and use the integration tests in
tests/integration/test_delivery_metrics.py to verify the SQL side.
"""

from __future__ import annotations

from app.db.repositories.delivery_metrics import _bucket_of


def test_no_wamid_with_send_error_classified_as_failed_pre_meta():
    """Outbound row written when Meta rejected the send before
    issuing a wamid. Operationally distinct from a Meta-side
    failure — our pipeline broke, not the recipient state."""
    assert (
        _bucket_of(status=None, has_wamid=False, has_send_error=True)
        == "failed_pre_meta"
    )


def test_failed_status_classified_as_failed():
    """Meta accepted the send, then later sent a failed webhook —
    delivery error (blocked, opted out, re-engagement window, etc)."""
    assert (
        _bucket_of(status="failed", has_wamid=True, has_send_error=False)
        == "failed"
    )


def test_read_supersedes_delivered_bucket():
    """``read`` implies ``delivered`` — the read bucket is rolled
    up separately so the dashboard can show what fraction of
    delivered messages were actually opened."""
    assert _bucket_of("read", True, False) == "read"


def test_delivered_status_classified_as_delivered():
    assert _bucket_of("delivered", True, False) == "delivered"


def test_sent_status_classified_as_sent_only():
    """Meta accepted the send but no delivered/read webhook has
    arrived yet — message is in flight."""
    assert _bucket_of("sent", True, False) == "sent_only"


def test_wamid_present_no_status_classified_as_no_status_yet():
    """Edge case: brand-new send the webhook hasn't caught up to,
    or webhook delivery delay. Distinct from sent_only because the
    `sent` webhook itself hasn't arrived either."""
    assert _bucket_of(None, True, False) == "no_status_yet"


def test_no_wamid_no_send_error_classified_as_no_status_yet():
    """Pathological edge case — no wamid AND no _send_error means
    something is off in the logging path. Classify as no_status_yet
    rather than failed_pre_meta so we don't inflate failure metrics
    on logging bugs."""
    assert _bucket_of(None, False, False) == "no_status_yet"
