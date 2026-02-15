from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageCategory(str, Enum):
    UTILITY = "utility"
    SERVICE = "service"
    MARKETING = "marketing"
    AUTHENTICATION = "authentication"


class IntentType(str, Enum):
    ADHERENCE_UPDATE = "adherence_update"
    REGIMEN_CREATE = "regimen_create"
    REFILL_REQUEST = "refill_request"
    SYMPTOM_REPORT = "symptom_report"
    PREGNANCY_CHECKLIST = "pregnancy_checklist"
    CAREGIVER_ADMIN = "caregiver_admin"
    GENERAL_QUESTION = "general_question"


class MessageIn(BaseModel):
    message_id: str
    patient_id: str
    phone: str
    channel: Literal["whatsapp"] = "whatsapp"
    text: str | None = None
    voice_url: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class QuickReply(BaseModel):
    id: str
    title: str


class MessageOut(BaseModel):
    patient_id: str
    phone: str
    body: str
    use_template: bool = False
    template_name: str | None = None
    category: MessageCategory = MessageCategory.UTILITY
    quick_replies: list[QuickReply] = Field(default_factory=list)
    correlation_id: str | None = None


class EventType(str, Enum):
    DOSE_DUE = "dose_due"
    DOSE_CONFIRMED = "dose_confirmed"
    DOSE_MISSED = "dose_missed"
    REFILL_DUE = "refill_due"
    TRIAGE_ALERT = "triage_alert"


class Event(BaseModel):
    event_type: EventType
    patient_id: str
    at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
