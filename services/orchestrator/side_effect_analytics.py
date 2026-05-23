"""Side-effect frequency analytics — clinical pattern detection
across the patient panel.

Builds on the side_effect_report tickets the side_effect_handler
opens. Doctors looking at a single ticket see one patient's
report; analytics across all reports surface patterns the doctor
would never spot manually:

    "3 of 12 patients on metformin reported nausea this month."
    "Adverse reactions in the diabetes cohort doubled vs. last 30 days."
    "Top symptom across panel: dizziness (8 reports, 5 patients)."

Methodology:

    Reports are unstructured — the verbatim "Patient said:" block
    is free-form text. To attribute a report to a medication, we
    cross-reference the report's text against the patient's
    REGIMENS active at the time of the report. A report mentioning
    "metformin" attributes to that regimen if the patient was
    actively on metformin when they filed the report.

    This is a heuristic — a report saying "nausea" without
    naming a drug attributes to NONE of the patient's regimens
    (uncategorized). False negatives are acceptable; false
    POSITIVES (attributing a report to a drug the patient
    isn't on) would be misleading, so the regimen check is
    strict.

Scope (v1):

    - per-medication report + patient counts, top-3 symptoms
    - per-cohort (legacy diabetes/cardiac/fall_risk)
    - top-N symptoms across all reports
    - summary tiles (total, unique patients, unique meds)

    Out of scope (v2.1):
    - week-over-week trend / time series
    - LLM-extracted structured fields (severity, body system, etc.)
    - cohort_tags-based grouping (we use the legacy boolean
      cohorts here for simplicity; tags are richer but require
      a join)
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OpsTicket, Patient, Regimen

log = logging.getLogger(__name__)


# Symptom vocabulary — the words we tag verbatim text with for
# the "top symptoms" rollup. Each entry is the canonical bucket
# label paired with a tuple of regex-friendly aliases that map to
# it. Aliases let a report saying "vomited" group with one saying
# "vomiting" without exploding the bucket count.
#
# Mirrors the patterns the side_effect_handler matcher uses — we
# don't want to detect a report (handler) and tag a report
# (analytics) with different vocabularies.
_SYMPTOM_VOCABULARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nausea", ("nausea", "nauseous", "nauseated")),
    ("vomiting", ("vomiting", "vomit", "vomited", "throwing up", "threw up")),
    ("dizziness", ("dizzy", "dizziness", "lightheaded", "light-headed")),
    ("headache", ("headache", "headaches", "migraine", "migraines")),
    ("rash", ("rash", "rashes", "hives")),
    ("itching", ("itching", "itchy", "itch")),
    ("swelling", ("swelling", "swollen")),
    ("drowsiness", ("drowsy", "drowsiness", "sleepy")),
    ("fatigue", ("fatigue", "exhaustion", "exhausted", "tired", "tiredness")),
    ("stomach pain", ("stomach pain", "stomach ache", "abdominal pain", "stomach cramps")),
    ("chest pain", ("chest pain", "chest tightness", "chest discomfort")),
    (
        "breathing issues",
        ("breathing problems", "breathing issues", "difficulty breathing", "shortness of breath"),
    ),
)


def _extract_symptoms(text: str | None) -> list[str]:
    """Return canonical symptom labels mentioned in ``text``.

    Each label appears at most once in the result regardless of
    how many alias matches fire — the analytics roll-up wants
    "this report mentioned dizziness" as a binary signal, not
    "this report mentioned dizziness six times". Order is the
    vocabulary's declared order so the output is deterministic
    across runs.
    """
    if not text:
        return []
    haystack = text.lower()
    matches: list[str] = []
    for label, aliases in _SYMPTOM_VOCABULARY:
        for alias in aliases:
            # Word-boundary anchored so "rash" doesn't match
            # "harassment" and "vomit" doesn't match "vomiteer"
            # (or whatever).
            if re.search(rf"\b{re.escape(alias)}\b", haystack):
                matches.append(label)
                break
    return matches


def _extract_medications(
    text: str | None, *, regimen_meds: list[str]
) -> list[str]:
    """Cross-reference ``text`` against the patient's active
    regimen medication names. Returns the canonical names that
    appear in the report.

    Strict matching: a report mentioning "metformin" attributes
    to that regimen ONLY if the patient was on metformin at the
    time of report. We deliberately don't try to extract
    medication names from text directly (NER) — the regimen
    cross-reference catches the high-confidence cases without
    risking false-positive attribution to drugs the patient
    isn't actually on.
    """
    if not text or not regimen_meds:
        return []
    haystack = text.lower()
    out: list[str] = []
    for med in regimen_meds:
        med_clean = (med or "").strip()
        if not med_clean:
            continue
        # Word-boundary so "atorvastatin" doesn't match a
        # substring of some unrelated word. The medication name
        # is taken verbatim from the regimen row.
        if re.search(
            rf"\b{re.escape(med_clean.lower())}\b", haystack
        ):
            out.append(med_clean)
    return out


# ---- Aggregation ---------------------------------------------------------


@dataclass
class MedicationStat:
    medication_name: str
    report_count: int
    patient_count: int
    top_symptoms: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class CohortStat:
    cohort: str
    report_count: int
    patient_count: int


@dataclass
class SymptomStat:
    symptom: str
    count: int


@dataclass
class SideEffectAnalytics:
    since: datetime
    until: datetime
    total_reports: int
    unique_patients: int
    unique_medications: int
    by_medication: list[MedicationStat] = field(default_factory=list)
    by_cohort: list[CohortStat] = field(default_factory=list)
    top_symptoms: list[SymptomStat] = field(default_factory=list)


_DEFAULT_WINDOW_DAYS = 30
_MAX_MEDICATIONS = 20
_MAX_TOP_SYMPTOMS = 12


def _import_extractor():
    """Lazy import of the notes extractor from main.py to break
    the circular import (main imports analytics, analytics needs
    the extractor)."""
    from services.orchestrator.main import _extract_reported_text

    return _extract_reported_text


async def compute_side_effect_analytics(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> SideEffectAnalytics:
    """Aggregate side-effect reports across the panel into clinical
    pattern signal. Returns a ``SideEffectAnalytics`` regardless
    of whether any reports exist in the window — empty buckets
    render as "no data" in the UI rather than missing fields."""
    extract_text = _import_extractor()

    when = until or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    window_start = since or (when - timedelta(days=_DEFAULT_WINDOW_DAYS))
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)

    # Pull all side-effect reports in the window. Each ticket
    # carries the patient phone + verbatim notes — that's the
    # input to attribution. One query for the panel.
    ticket_stmt = (
        select(OpsTicket)
        .where(OpsTicket.category == "side_effect_report")
        .where(OpsTicket.created_at >= window_start)
        .where(OpsTicket.created_at <= when)
        .order_by(desc(OpsTicket.created_at))
    )
    tickets = list((await db.execute(ticket_stmt)).scalars().all())

    if not tickets:
        return SideEffectAnalytics(
            since=window_start,
            until=when,
            total_reports=0,
            unique_patients=0,
            unique_medications=0,
        )

    # Resolve phone → patient row + active regimens. Two queries:
    # one for patients (by phone), one for regimens (by patient id).
    phones = list({t.patient_id for t in tickets})
    patients_stmt = select(Patient).where(Patient.phone.in_(phones))
    patient_rows = list(
        (await db.execute(patients_stmt)).scalars().all()
    )
    patient_by_phone: dict[str, Patient] = {
        p.phone: p for p in patient_rows if p.phone
    }
    patient_ids = [p.id for p in patient_rows]

    regimens_by_patient: dict[int, list[Regimen]] = defaultdict(list)
    if patient_ids:
        regimen_stmt = select(Regimen).where(
            Regimen.patient_id.in_(patient_ids)
        )
        for r in (await db.execute(regimen_stmt)).scalars().all():
            regimens_by_patient[r.patient_id].append(r)

    # Walk each ticket: extract verbatim text, attribute mentions,
    # accumulate per-medication + per-cohort + symptom counters.
    med_reports: defaultdict[str, set[int]] = defaultdict(set)
    med_report_counts: defaultdict[str, int] = defaultdict(int)
    med_symptom_counts: defaultdict[
        str, defaultdict[str, int]
    ] = defaultdict(lambda: defaultdict(int))
    cohort_reports: defaultdict[str, set[int]] = defaultdict(set)
    cohort_counts: defaultdict[str, int] = defaultdict(int)
    symptom_counts: defaultdict[str, int] = defaultdict(int)
    unique_patient_ids: set[int] = set()
    unique_medications: set[str] = set()

    for ticket in tickets:
        patient = patient_by_phone.get(ticket.patient_id)
        if patient is None:
            # Erased / unknown patient — count the report toward
            # totals but skip per-patient + per-medication
            # attribution. Without a patient row we can't
            # cross-reference regimens.
            text = extract_text(ticket.notes) or ""
            for symptom in _extract_symptoms(text):
                symptom_counts[symptom] += 1
            continue
        unique_patient_ids.add(patient.id)

        verbatim = extract_text(ticket.notes) or ""
        regimen_med_names = [
            r.medication_name
            for r in regimens_by_patient.get(patient.id, [])
            if r.medication_name
        ]

        attributed_meds = _extract_medications(
            verbatim, regimen_meds=regimen_med_names
        )
        symptoms = _extract_symptoms(verbatim)

        # Top-symptoms (panel-wide) — every report contributes.
        for sym in symptoms:
            symptom_counts[sym] += 1

        # Per-medication: only reports that mention an active
        # regimen-medication contribute. Reports without a
        # mentioned medication go to the symptom roll-up but
        # NOT the per-medication breakdown.
        for med in attributed_meds:
            med_reports[med].add(patient.id)
            med_report_counts[med] += 1
            unique_medications.add(med)
            for sym in symptoms:
                med_symptom_counts[med][sym] += 1

        # Per-cohort attribution. A patient can be in multiple
        # cohorts (e.g. diabetic + cardiac); each cohort gets
        # credit for the report. Patients in NO cohort are
        # tallied under "uncategorized" so the doctor sees
        # everything.
        in_cohort = False
        for label, flag in (
            ("diabetes", patient.cohort_diabetes),
            ("cardiac", patient.cohort_cardiac),
            ("fall_risk", patient.cohort_fall_risk),
        ):
            if flag:
                cohort_reports[label].add(patient.id)
                cohort_counts[label] += 1
                in_cohort = True
        if not in_cohort:
            cohort_reports["uncategorized"].add(patient.id)
            cohort_counts["uncategorized"] += 1

    # Build the by_medication list, sorted by report count desc.
    medication_stats: list[MedicationStat] = []
    for med in sorted(
        med_report_counts,
        key=lambda m: (-med_report_counts[m], m),
    ):
        symptoms_for_med = sorted(
            med_symptom_counts[med].items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:3]
        medication_stats.append(
            MedicationStat(
                medication_name=med,
                report_count=med_report_counts[med],
                patient_count=len(med_reports[med]),
                top_symptoms=symptoms_for_med,
            )
        )
    medication_stats = medication_stats[:_MAX_MEDICATIONS]

    cohort_stats = [
        CohortStat(
            cohort=label,
            report_count=cohort_counts[label],
            patient_count=len(cohort_reports[label]),
        )
        for label in sorted(
            cohort_counts,
            key=lambda c: (-cohort_counts[c], c),
        )
    ]

    top_symptoms = [
        SymptomStat(symptom=sym, count=cnt)
        for sym, cnt in sorted(
            symptom_counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:_MAX_TOP_SYMPTOMS]
    ]

    return SideEffectAnalytics(
        since=window_start,
        until=when,
        total_reports=len(tickets),
        unique_patients=len(unique_patient_ids),
        unique_medications=len(unique_medications),
        by_medication=medication_stats,
        by_cohort=cohort_stats,
        top_symptoms=top_symptoms,
    )
