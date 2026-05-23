import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env so DATABASE_URL etc. are visible at pytest collection time
# (skipif markers evaluate before any test module imports app.db.session).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Test-profile pool sizing — bumped from prod's 5+5 because
# the integration suite holds many module-scoped TestClient
# fixtures concurrently in one process. See ``app/db/session``
# for the rationale. Override via env if a CI runner has a
# tighter pgbouncer cap. ``setdefault`` so an explicit
# DB_POOL_SIZE override still wins.
os.environ.setdefault("DB_POOL_SIZE", "10")
os.environ.setdefault("DB_MAX_OVERFLOW", "10")
os.environ.setdefault("DB_POOL_RECYCLE", "180")

# Force the WhatsApp gateway into dry-run for the test session so /send
# never hits the real Meta Graph API (which 4xx's on dev tokens / test
# templates). Dry-run still writes the outbound to message_log and returns
# a synthetic accepted result, so delivery-path assertions hold. HARD set
# (not setdefault) — .env ships WHATSAPP_DRY_RUN=0 for real dev sends and
# `source .env` exports it before pytest, so a default wouldn't win. Same
# safety-override pattern as LLM_ENABLED below. To exercise a real sandbox
# send, comment this out.
os.environ["WHATSAPP_DRY_RUN"] = "1"

# Disable the LLM by default for the test session — existing assertions
# expect the deterministic rule-based outputs. Tests that exercise the LLM
# (tests/integration/test_llm_workflow.py, tests/test_llm_unit.py) opt in
# via fixtures that re-enable it.
os.environ["LLM_ENABLED"] = "0"

# Disable the compiled LangGraph (PostgresSaver) by default. The lifespan
# would otherwise try to open a checkpoint pool and run setup() on every
# orchestrator TestClient instantiation, slowing the suite and pulling in
# DB writes the unit tests don't expect. Integration tests that need the
# compiled graph re-enable it via fixtures.
os.environ["LANGGRAPH_ENABLED"] = "0"

# Reset any cached LLM singleton that may already be enabled if a prior
# in-process import loaded the module before this conftest ran.
try:
    from services.orchestrator.llm import reset_llm_for_tests

    reset_llm_for_tests()
except Exception:
    pass


# ---- Sync-test event-loop shim ---------------------------------------------
# Several legacy integration tests are written as ``def test_xxx(...)``
# (sync) but call into async helpers via
# ``asyncio.get_event_loop().run_until_complete(...)``. Python 3.12
# deprecated ``get_event_loop()`` returning a freshly-created loop when
# none is current; in mixed sync+async test files the loop pytest-asyncio
# spins up for async tests gets closed between cases, so the next sync
# test finds no loop and ``run_until_complete`` either raises or hangs.
#
# Without rewriting ~14 sync-test files, the workaround is an autouse
# function-scoped fixture that ensures a fresh, open event loop is
# installed for EACH test. Async tests get a separate loop from
# pytest-asyncio's plugin tree (this fixture's loop isn't bound to
# them); sync tests calling ``asyncio.get_event_loop()`` find the
# installed loop and ``run_until_complete`` works.
import asyncio  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_default_event_loop():
    """Install a usable event loop for sync tests calling
    ``asyncio.get_event_loop().run_until_complete(...)``. Per-
    test scope so a closed loop from a prior test doesn't leak."""
    try:
        existing = asyncio.get_event_loop_policy().get_event_loop()
        if existing.is_closed():
            raise RuntimeError("existing loop is closed")
    except (RuntimeError, DeprecationWarning):
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


# ---- Test data cleanup -----------------------------------------------------
# The integration suite runs against a remote Supabase Postgres and many
# test files seed patients without per-test cleanup. After 18 V2 slices
# the test DB has accumulated thousands of leftover rows — broadcast
# tests that resolve a cohort find 200+ historical diabetic test
# patients, blowing past LIMIT clauses and breaking assertions.
#
# This fixture prunes accumulated test rows at session start. It is
# safety-gated by ``MEDAGENT_TEST_CLEANUP=1`` (default OFF) so an
# accidental run against a non-test DATABASE_URL doesn't wipe rows.
# The regex matches phone columns shaped like the test seeders use
# (``[a-z][a-z0-9_-]*-[a-f0-9]{4,16}``) — real phones (``+91...`` or
# ``91...``) won't match.


@pytest.fixture(scope="session", autouse=True)
def _cleanup_accumulated_test_rows():
    """Wipe leftover test patients before the suite runs.

    Yields immediately; the cleanup happens before fixture
    teardown of any in-test resources. Per-session scope means
    we only do this once per pytest invocation, not once per
    file.
    """
    if os.getenv("MEDAGENT_TEST_CLEANUP", "0") != "1":
        # Default behaviour — no cleanup. Tests must tolerate
        # accumulated state, OR the runner explicitly opts in.
        yield
        return
    if not os.getenv("DATABASE_URL"):
        yield
        return

    async def _run() -> None:
        # Import here so collection-time conftest doesn't pull
        # in the SQLAlchemy graph for unit-test-only runs.
        from sqlalchemy import text as sql_text

        from app.db.session import get_engine

        engine = get_engine()
        # Phone regex: starts with lowercase letter, contains
        # lowercase/digits/_-, ends with a hex suffix. Won't
        # match real numbers (``+91...``). Anchored.
        phone_re = r"^[a-z][a-z0-9_-]*-[a-f0-9]{4,16}$"
        async with engine.begin() as conn:
            # Patient cascade handles caregivers, regimens,
            # adherence_events, lab_followups, appointments,
            # broadcast_sends.patient_db_id, clinical_alerts,
            # visit_briefs, care_plan_goals,
            # metric_observations.
            result = await conn.execute(
                sql_text(
                    "DELETE FROM patients WHERE phone ~ :rx "
                    "AND created_at < NOW() - INTERVAL '15 minutes'"
                ),
                {"rx": phone_re},
            )
            patient_deleted = result.rowcount or 0
            # Also wipe ops_tickets keyed to test phones —
            # ops_tickets.patient_id is a phone string, not a
            # FK, so CASCADE doesn't touch them.
            tk = await conn.execute(
                sql_text(
                    "DELETE FROM ops_tickets WHERE patient_id ~ :rx"
                ),
                {"rx": phone_re},
            )
            ticket_deleted = tk.rowcount or 0
            # Message log + audit + inbound_classifications
            # also key on phone strings.
            ml = await conn.execute(
                sql_text(
                    "DELETE FROM message_log WHERE patient_id ~ :rx"
                ),
                {"rx": phone_re},
            )
            ml_deleted = ml.rowcount or 0
            ic = await conn.execute(
                sql_text(
                    "DELETE FROM inbound_classifications WHERE patient_phone ~ :rx"
                ),
                {"rx": phone_re},
            )
            ic_deleted = ic.rowcount or 0
            # Doctors with test-shaped emails (used by recap +
            # appointment + on-call tests).
            dr = await conn.execute(
                sql_text(
                    "DELETE FROM doctors WHERE email LIKE '%@test.local' "
                    "OR email LIKE '%@example.com' OR email LIKE '%@x.test'"
                )
            )
            doctor_deleted = dr.rowcount or 0

            # Care plans accumulate badly — they're cohort-
            # global so tests that create them iterate ALL of
            # them on every sweep. test_care_plans /
            # test_care_gaps / test_visit_brief_scheduling all
            # create plans with predictable test-name shapes.
            # Real plans (the 3 seeded by initial migration:
            # ``HbA1c``, ``Blood pressure check``,
            # ``Vitamin D level``) don't match these patterns.
            cp = await conn.execute(
                sql_text(
                    "DELETE FROM care_plans WHERE "
                    "test_name LIKE 'Test %' OR "
                    "test_name LIKE 'Sweep Test%' OR "
                    "test_name LIKE 'Tag Test%' OR "
                    "test_name LIKE 'pre-visit-test-%' OR "
                    "test_name LIKE 'Live UI test%'"
                )
            )
            care_plan_deleted = cp.rowcount or 0

            # scheduled_events accumulate to thousands —
            # broadcast_send, recap_ack_nudge, lab_followup_due,
            # clinical_alert_page from V2 slice tests. The
            # scheduler / dispatcher / scheduled-event tests
            # call ``fetch_due()`` which scans ALL due events
            # globally; a few thousand stale rows push the
            # test's seeded event past LIMIT and fail the
            # assertion.
            #
            # Two-phase delete: (1) anything older than 1 day
            # is unambiguously residue; (2) anything in the
            # ``broadcast_send`` / ``recap_ack_nudge`` /
            # ``lab_followup_due`` / ``clinical_alert_page``
            # event types older than 1 hour, since these are
            # the test-heavy types and 1h is well past the
            # ``15-min'' patient safety window we apply
            # elsewhere.
            se_old = await conn.execute(
                sql_text(
                    "DELETE FROM scheduled_events "
                    "WHERE created_at < NOW() - INTERVAL '1 day'"
                )
            )
            se_test = await conn.execute(
                sql_text(
                    "DELETE FROM scheduled_events WHERE "
                    "event_type IN ('broadcast_send', "
                    "'recap_ack_nudge', 'lab_followup_due', "
                    "'clinical_alert_page', 'visit_brief_generate', "
                    "'recon_test', 'health_test_event', "
                    "'triage_alert') AND "
                    "created_at < NOW() - INTERVAL '1 hour'"
                )
            )
            se_deleted = (se_old.rowcount or 0) + (
                se_test.rowcount or 0
            )

            # Cohort tags created by tests — test_cohort_tags +
            # test_care_plans create these and they accumulate.
            ct = await conn.execute(
                sql_text(
                    "DELETE FROM cohort_tags WHERE "
                    "label LIKE 'Test%' OR label LIKE 'test%' OR "
                    "slug LIKE 'test-%' OR slug LIKE 'tag-%'"
                )
            )
            cohort_tag_deleted = ct.rowcount or 0
        print(
            f"\n[conftest] test-row cleanup: patients={patient_deleted} "
            f"tickets={ticket_deleted} message_log={ml_deleted} "
            f"inbound_class={ic_deleted} doctors={doctor_deleted} "
            f"care_plans={care_plan_deleted} "
            f"scheduled_events={se_deleted} "
            f"cohort_tags={cohort_tag_deleted}"
        )

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — never fatal
        print(
            f"[conftest] test-row cleanup failed (non-fatal): {exc}"
        )
    yield


# Test modules that run global-state sweeps (scan whole tables). Two of these
# running at once cross-contaminate each other's counts, so they're pinned to a
# single xdist worker. Add a module here (or tag a test ``@pytest.mark.serial``)
# when it asserts on table-wide aggregates rather than its own seeded rows.
_SERIAL_MODULES: frozenset[str] = frozenset(
    {
        "test_goal_drift_sweep",
        "test_adherence_pattern_sweep",
        "test_care_gaps",
        "test_recap_sweeps",
        "test_service_health_reconciler",
        "test_weekly_trend",
        "test_clinical_alert_pager",  # the re-page sweep scans all unacked alerts
    }
)


def pytest_collection_modifyitems(config, items):
    """Pin global-state tests to a single xdist group.

    The integration suite shares one remote Postgres. Most tests seed
    uniquely-suffixed rows and assert on those specific rows, so they're
    parallel-safe. The exceptions are *global-state* sweeps that scan whole
    tables — running two of those at once cross-contaminates counts.

    We auto-tag the known sweep modules (``_SERIAL_MODULES``) plus any test
    explicitly marked ``@pytest.mark.serial`` and give them a shared
    ``xdist_group``. Under ``pytest -n auto --dist loadgroup`` they all route
    to ONE worker (serial relative to each other) while the rest of the suite
    fans out. No-op on a non-xdist run.
    """
    for item in items:
        explicit = item.get_closest_marker("serial") is not None
        in_serial_module = any(
            f"{mod}.py::" in item.nodeid for mod in _SERIAL_MODULES
        )
        if explicit or in_serial_module:
            item.add_marker(pytest.mark.serial)
            item.add_marker(pytest.mark.xdist_group("serial"))
