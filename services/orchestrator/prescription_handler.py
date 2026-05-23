"""Deterministic handler for inbound prescription photos.

The Next.js webhook downloads the inbound image to
``apps/ops_console/public/uploads/prescriptions/<uuid>.jpg`` and rewrites
the inbound text to a marker like::

    [prescription-upload] public_path=/uploads/prescriptions/abc.jpg mime=image/jpeg

This handler:

  1. Validates the marker + extracts ``public_path`` (and optional caption).
  2. Creates a ``prescriptions`` row (status=pending).
  3. Calls the vision LLM (``AgentLLM.parse_prescription_image``) against
     the public URL composed of ``PUBLIC_BASE_URL + public_path``. The
     parser must be reachable from OpenAI's network, so this only works
     when ``PUBLIC_BASE_URL`` is the cloudflared / public domain (defaults
     to ``https://webhook.bullionbrains.com``).
  4. Stores the structured ``ParsedPrescription`` dump in
     ``parsed_payload`` for the ops console review UI.
  5. Replies to the patient acknowledging receipt and noting that a
     clinician will review.

Failures (LLM unreachable, vision parse error) leave ``parsed_payload``
empty — the clinician can still enter regimens manually in the ops
console. Patient still gets the ack so they know we received the image.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.db.repositories import patients as patients_repo
from app.db.repositories import prescriptions as prescriptions_repo
from app.db.session import get_sessionmaker
from services.orchestrator.llm import get_llm

log = logging.getLogger(__name__)


_DEFAULT_PUBLIC_BASE = "https://webhook.bullionbrains.com"


def _public_base_url() -> str:
    return (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("WHATSAPP_PUBLIC_BASE_URL")
        or _DEFAULT_PUBLIC_BASE
    ).rstrip("/")


_PRESCRIPTION_RE = re.compile(
    r"^\s*\[prescription-upload\]\s+"
    r"public_path=(?P<path>\S+)"
    r"(?:\s+mime=(?P<mime>\S+))?"
    r"(?:\s+caption=(?P<caption>\".*?\"|\S+))?"
    r"\s*$"
)


def looks_like_prescription_upload(text: str) -> bool:
    return bool(text and _PRESCRIPTION_RE.match(text))


async def handle_prescription_upload(
    *,
    patient_phone: str,
    new_user_text: str,
) -> dict[str, Any] | None:
    match = _PRESCRIPTION_RE.match(new_user_text or "")
    if match is None:
        return None
    public_path = match.group("path")
    mime = match.group("mime") or "image/jpeg"
    caption_raw = match.group("caption")
    caption = (
        caption_raw.strip('"').replace('\\"', '"') if caption_raw else None
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            log.warning(
                "prescription handler: patient %s not found", patient_phone
            )
            return _reply(
                "I couldn't find your account. Please contact support.",
                audit_codes=["prescription_unknown_patient"],
            )

        public_url = f"{_public_base_url()}{public_path}"

        # Pre-populate parsed_payload with metadata before the LLM call so
        # the row exists even if vision parsing fails. The vision result
        # gets merged in a moment.
        prescription = await prescriptions_repo.create(
            db,
            patient_id=patient.id,
            source_upload_url=public_url,
            parsed_payload={
                "public_path": public_path,
                "mime_type": mime,
                "caption": caption,
                "vision_parse_attempted": False,
            },
        )
        await db.commit()

        # Fire the vision LLM. On failure we leave the row in a
        # "needs-manual-entry" state — the ops console handles that.
        llm = get_llm()
        result = await llm.parse_prescription_image(image_url=public_url)
        if result is None:
            log.warning(
                "prescription %s: vision parse failed (LLM unreachable / error)",
                prescription.id,
            )
            await prescriptions_repo.set_parsed_payload(
                db,
                prescription.id,
                parsed_payload={
                    **(prescription.parsed_payload or {}),
                    "vision_parse_attempted": True,
                    "vision_parse_failed": True,
                },
            )
            await db.commit()
            return _reply(
                "Got your prescription. Our team will review it manually "
                "and set up your reminders shortly. ✓",
                audit_codes=["prescription_received_no_parse"],
            )

        # Persist the structured parse alongside the metadata.
        parsed_dump = result.parsed.model_dump()
        await prescriptions_repo.set_parsed_payload(
            db,
            prescription.id,
            parsed_payload={
                "public_path": public_path,
                "mime_type": mime,
                "caption": caption,
                "vision_parse_attempted": True,
                "vision_parse_failed": False,
                "vision_model": result.used_model,
                "parsed": parsed_dump,
            },
        )
        await db.commit()

        # Patient-facing confirmation. Keep it grounded in what we actually
        # found so the patient can correct us if it's clearly wrong.
        regimen_count = len(parsed_dump.get("regimens", []))
        if parsed_dump.get("illegible") or regimen_count == 0:
            body = (
                "Got your prescription, but parts were hard to read. Our "
                "team will review it manually and set up your reminders "
                "shortly. ✓"
            )
            audit = ["prescription_received_illegible"]
        else:
            meds = ", ".join(
                r["medication_name"]
                for r in parsed_dump["regimens"][:3]
            )
            extra = (
                f" (+{regimen_count - 3} more)"
                if regimen_count > 3
                else ""
            )
            body = (
                f"Got your prescription. I see: {meds}{extra}. Our team "
                f"will confirm the details and your reminders will start "
                f"once it's verified. ✓"
            )
            audit = ["prescription_received_parsed"]
        return _reply(body, audit_codes=audit)


def _reply(body: str, *, audit_codes: list[str]) -> dict[str, Any]:
    return {
        "response_body": body,
        "audit_reasons": audit_codes,
        "intent": "general_question",
        "risk_level": "low",
        "escalation_required": False,
        "use_template": False,
        "template_name": None,
        "quick_replies": ["CALL", "HELP"],
        "buttons": [],
        "list_rows": [],
        "list_button_label": None,
        "list_section_title": None,
    }
