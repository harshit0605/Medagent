"""Patient self-reported vitals capture.

The SoT's daily-retention engine: a patient texts a reading
("sugar 140", "BP 130/85", "weight 72kg") and we log it as a
``metric_observation`` (source=``patient_self_report``), link
it to a matching active care-plan goal when one exists, and
reply with an acknowledgement + on/off-target context + a
light weekly count.

Design notes:
- **Regex parsing, not LLM.** Vitals are short, structured,
  high-volume, and safety-adjacent — a deterministic parser is
  cheaper, faster, and won't hallucinate a number. Each metric
  has a keyword-anchored pattern with a plausibility range so
  "I take 2 tablets" or "call me at 9" don't register as
  readings.
- **Storage already exists** (slice 14: ``metric_observations``
  + ``care_plan_goals``). This module is the missing inbound
  capture path that the ``patient_self_report`` source value
  was always meant to feed.
- **Routing**: ranked AFTER side-effect/symptom detection in
  the agent workflow so "my sugar is 400 and I feel dizzy"
  routes to clinical triage first (safety wins), vitals second.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedVital:
    metric_key: str
    metric_label: str
    value: Decimal
    unit: str


# Each entry: (metric_key, label, unit, compiled regex, (lo, hi) plausible
# range). The regex's first capture group is the numeric value. BP is handled
# separately because it yields two readings from one match.
_GLUCOSE_RE = re.compile(
    r"(?:blood\s*sugar|sugar\s*level|sugar|glucose|fasting\s*(?:sugar|glucose)|\bbs\b|\bbgl\b)"
    r"[^\d]{0,8}(\d{2,3})(?:\s*(?:mg/?dl|mgdl))?",
    re.IGNORECASE,
)
_HBA1C_RE = re.compile(
    r"(?:hba1c|a1c)[^\d]{0,6}(\d{1,2}(?:\.\d)?)\s*%?",
    re.IGNORECASE,
)
_WEIGHT_RE = re.compile(
    r"(?:weight|\bwt\b)[^\d]{0,6}(\d{2,3}(?:\.\d)?)\s*(?:kg|kgs|kilo(?:gram)?s?)?",
    re.IGNORECASE,
)
_PEAKFLOW_RE = re.compile(
    r"(?:peak\s*flow|\bpef\b)[^\d]{0,8}(\d{2,3})",
    re.IGNORECASE,
)
# BP: a systolic/diastolic pair. Accept with or without a keyword, but only
# when the slash-number shape is unambiguous and both numbers are in range.
_BP_RE = re.compile(
    r"(?:bp|blood\s*pressure)?[^\d]{0,8}(\d{2,3})\s*/\s*(\d{2,3})",
    re.IGNORECASE,
)


def _in_range(v: Decimal, lo: float, hi: float) -> bool:
    return Decimal(str(lo)) <= v <= Decimal(str(hi))


def parse_vitals(text: str) -> list[ParsedVital]:
    """Extract every recognised vitals reading from ``text``.

    Returns a list (BP yields two entries — systolic + diastolic).
    Empty list when nothing parses as a plausible reading.
    """
    if not text:
        return []
    out: list[ParsedVital] = []

    # --- Blood pressure (most specific shape: NNN/NNN) ---
    bp_match = _BP_RE.search(text)
    if bp_match:
        sys_v = Decimal(bp_match.group(1))
        dia_v = Decimal(bp_match.group(2))
        # Plausible adult BP. The slash + both-in-range is a strong signal
        # even without an explicit "BP" keyword (dates like 12/2026 fail the
        # diastolic range; ratios like 1/2 fail systolic range).
        if _in_range(sys_v, 70, 260) and _in_range(dia_v, 40, 160):
            out.append(ParsedVital("bp_systolic", "Systolic BP", sys_v, "mmHg"))
            out.append(
                ParsedVital("bp_diastolic", "Diastolic BP", dia_v, "mmHg")
            )

    # --- Blood glucose ---
    m = _GLUCOSE_RE.search(text)
    if m:
        v = Decimal(m.group(1))
        if _in_range(v, 40, 600):
            out.append(
                ParsedVital("blood_glucose", "Blood glucose", v, "mg/dL")
            )

    # --- HbA1c ---
    m = _HBA1C_RE.search(text)
    if m:
        v = Decimal(m.group(1))
        if _in_range(v, 3, 20):
            out.append(ParsedVital("hba1c", "HbA1c", v, "%"))

    # --- Weight --- (the regex already requires a weight/wt keyword)
    m = _WEIGHT_RE.search(text)
    if m:
        v = Decimal(m.group(1))
        if _in_range(v, 20, 300):
            out.append(ParsedVital("weight_kg", "Weight", v, "kg"))

    # --- Peak flow ---
    m = _PEAKFLOW_RE.search(text)
    if m:
        v = Decimal(m.group(1))
        if _in_range(v, 50, 900):
            out.append(ParsedVital("peak_flow", "Peak flow", v, "L/min"))

    return out


def looks_like_vitals_log(text: str | None) -> bool:
    """Cheap gate for the router — true when at least one plausible
    reading parses out of the message."""
    if not text:
        return False
    return len(parse_vitals(text)) > 0


def _on_target_note(goal: Any, value: Decimal) -> str:
    """Short on/off-target clause for the ack, given a matched goal."""
    op = "<" if goal.comparator == "less_than" else ">"
    target = goal.target_value
    if goal.comparator == "less_than":
        ok = value < target
    elif goal.comparator == "greater_than":
        ok = value > target
    else:
        ok = False
    target_str = f"target {op} {target} {goal.target_unit}"
    return (
        f" — on track ({target_str}) ✓"
        if ok
        else f" — above {target_str}, keep going"
        if goal.comparator == "less_than"
        else f" — below {target_str}, keep going"
    )


async def handle_vitals_log(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Parse + persist self-reported vitals. Returns a workflow delta
    (``response_body`` + ``audit_reasons``) on success, or ``None`` when
    there's no patient row or nothing parses (so the caller falls through
    to the normal flow)."""
    from app.db.repositories import (
        care_plan_goals as goals_repo,
        patients as patients_repo,
    )
    from app.db.session import get_sessionmaker

    readings = parse_vitals(new_user_text)
    if not readings:
        return None

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None

        # Active goals, indexed by metric_key for O(1) match per reading.
        active_goals = await goals_repo.list_goals_for_patient(
            db, patient.id, status="active"
        )
        goal_by_metric = {g.metric_key: g for g in active_goals}

        ack_lines: list[str] = []
        logged_metrics: list[str] = []
        for r in readings:
            goal = goal_by_metric.get(r.metric_key)
            await goals_repo.record_observation(
                db,
                patient_id=patient.id,
                goal_id=goal.id if goal is not None else None,
                metric_key=r.metric_key,
                value=r.value,
                unit=r.unit,
                source="patient_self_report",
                recorded_by=None,
                notes=None,
            )
            logged_metrics.append(r.metric_key)
            line = f"{r.metric_label}: {r.value} {r.unit} ✓"
            if goal is not None:
                line += _on_target_note(goal, r.value)
            ack_lines.append(line)

        # Light weekly-trend touch: count this patient's readings in the
        # last 7 days for the first logged metric. Cheap single query;
        # gives the patient a sense of momentum without a full chart.
        from datetime import datetime, timedelta, timezone

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        recent = await goals_repo.list_observations_for_patient(
            db, patient.id, metric_key=readings[0].metric_key, limit=50
        )
        week_count = sum(
            1
            for o in recent
            if (
                o.observed_at.replace(tzinfo=timezone.utc)
                if o.observed_at.tzinfo is None
                else o.observed_at
            )
            >= week_ago
        )

        await db.commit()

    body = "Logged your readings:\n" + "\n".join(
        f"• {line}" for line in ack_lines
    )
    if week_count > 1:
        body += (
            f"\n\nThat's {week_count} "
            f"{readings[0].metric_label.lower()} readings this week — "
            "nice consistency."
        )
    body += "\n\nReply with another reading anytime, or HELP for support."

    log.info(
        "vitals logged for %s: %s",
        patient_phone,
        ",".join(logged_metrics),
    )
    return {
        "response_body": body,
        "audit_reasons": ["vitals_self_report"],
    }
