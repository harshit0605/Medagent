from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .enums import (
    ChannelType,
    EventType,
    Intent,
    MessageCategory,
    MessageContentType,
    MessageMode,
)


class ChannelMetadata(BaseModel):
    channel_type: ChannelType
    provider: str
    source_address: str
    destination_address: str
    external_message_id: str | None = None


class PatientIdentity(BaseModel):
    patient_id: str
    patient_mrn: str | None = None
    timezone: str
    preferred_language: str = "en"


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    channel: ChannelMetadata
    patient: PatientIdentity
    content_type: MessageContentType
    text: str | None = None
    audio_url: HttpUrl | None = None
    audio_duration_seconds: int | None = Field(default=None, ge=0)
    intent: Intent = Intent.UNKNOWN
    received_at: datetime
    sent_at: datetime | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_content_payload(self) -> "MessageIn":
        if self.content_type == MessageContentType.TEXT and not self.text:
            raise ValueError("text is required when content_type is 'text'")
        if self.content_type == MessageContentType.AUDIO and not self.audio_url:
            raise ValueError("audio_url is required when content_type is 'audio'")
        return self
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


class ActionButton(BaseModel):
    id: str
    label: str
    action: str


class Destination(BaseModel):
    channel_type: ChannelType
    recipient_address: str


class MessageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    mode: MessageMode
    category: MessageCategory
    destination: Destination
    correlation_id: str
    template_name: str | None = None
    template_params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    quick_replies: list[QuickReply] = Field(default_factory=list)
    buttons: list[ActionButton] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def validate_template_fields(self) -> "MessageOut":
        if self.mode == MessageMode.TEMPLATE and not self.template_name:
            raise ValueError("template_name is required when mode is 'template'")
        return self


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: EventType
    patient_id: str
    occurred_at: datetime
    correlation_id: str | None = None


class DoseDueEvent(EventBase):
    event_type: Literal[EventType.DOSE_DUE]
    medication_name: str
    dose_instruction: str
    due_at: datetime


class DoseConfirmedEvent(EventBase):
    event_type: Literal[EventType.DOSE_CONFIRMED]
    medication_name: str
    confirmed_at: datetime
    confirmation_channel: ChannelType


class DoseMissedEvent(EventBase):
    event_type: Literal[EventType.DOSE_MISSED]
    medication_name: str
    scheduled_at: datetime
    grace_window_minutes: int = Field(ge=0)


class RefillDueEvent(EventBase):
    event_type: Literal[EventType.REFILL_DUE]
    medication_name: str
    refill_by: datetime
    remaining_doses: int = Field(ge=0)


class TriageAlertEvent(EventBase):
    event_type: Literal[EventType.TRIAGE_ALERT]
    severity: Literal["low", "medium", "high", "critical"]
    reason: str
    escalation_target: str


Event = Annotated[
    DoseDueEvent | DoseConfirmedEvent | DoseMissedEvent | RefillDueEvent | TriageAlertEvent,
    Field(discriminator="event_type"),
]
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
