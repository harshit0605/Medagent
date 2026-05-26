"""Wound-photo → clinician review queue (post-op, SoT §3D).

The ingress layer forwards a wound photo as a marker (mirroring the
prescription-image path)::

    [wound-photo] public_path=/uploads/wounds/abc.jpg mime=image/jpeg

A wound photo is a clinical-review artifact, not a symptom-triage event — we
don't want the LLM interpreting it. So we route it deterministically: open a
``wound_review`` ops ticket (idempotent per patient) carrying the image path,
and reply that the care team will review. A clinician views the photo + closes
the ticket from the ops console.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.logging_redact import redact_phone

log = logging.getLogger(__name__)

WOUND_REVIEW_CATEGORY = "wound_review"

_WOUND_RE = re.compile(
    r"^\s*\[wound-photo\]\s+public_path=(?P<path>\S+)"
    r"(?:\s+mime=(?P<mime>\S+))?\s*$",
    re.IGNORECASE,
)


def looks_like_wound_photo(text: str | None) -> bool:
    return bool(text and _WOUND_RE.match(text))


async def handle_wound_photo(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Open a wound-review ops ticket for the submitted photo. Returns a
    workflow delta or ``None`` when the text isn't a wound-photo marker."""
    from app.db.repositories import ops_tickets as ops_tickets_repo
    from app.db.repositories import patients as patients_repo
    from app.db.session import get_sessionmaker

    match = _WOUND_RE.match(new_user_text or "")
    if match is None:
        return None
    public_path = match.group("path")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return {
                "response_body": (
                    "Thanks for the photo — but we couldn't find your account. "
                    "Please contact support."
                ),
                "audit_reasons": ["wound_photo_unknown_patient"],
            }
        existing = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=patient_phone, category=WOUND_REVIEW_CATEGORY
        )
        if existing is None:
            await ops_tickets_repo.create(
                db,
                patient_id=patient_phone,
                category=WOUND_REVIEW_CATEGORY,
                priority="p2",
                sla_minutes=720,  # 12h — wound concerns shouldn't wait long
                notes=f"Wound photo submitted for review: {public_path}",
            )
        else:
            await ops_tickets_repo.append_note(
                db,
                existing.id,
                actor="system",
                note=f"Additional wound photo submitted: {public_path}",
            )
        await db.commit()

    log.info("wound photo queued for review: patient=%s", redact_phone(patient_phone))
    return {
        "response_body": (
            "Thanks — your wound photo is with the care team for review. "
            "We'll be in touch; reply HELP if it's urgent or you're in pain."
        ),
        "audit_reasons": ["wound_photo_received"],
    }
