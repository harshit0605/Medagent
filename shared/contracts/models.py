from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .enums import ChannelType, EventType, Intent, MessageCategory, MessageContentType, MessageMode


class IntentType(str, Enum):
    ADHERENCE_UPDATE = "adherence_update"
    REGIMEN_CREATE = "regimen_create"
    REFILL_REQUEST = "refill_request"
    SYMPTOM_REPORT = "symptom_report"
    PREGNANCY_CHECKLIST = "pregnancy_checklist"
    CAREGIVER_ADMIN = "caregiver_admin"
    GENERAL_QUESTION = "general_question"


class ChannelMetadata(BaseModel):
    channel_type: ChannelType
    provider: str
    source_address: str
    destination_address: str
    external_message_id: str | None = None


class PatientIdentity(BaseModel):
    patient_id: str
    patient_mrn: str | None = None
    timezone: str = "UTC"
    preferred_language: str = "en"


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    channel: ChannelMetadata | Literal["whatsapp"] = "whatsapp"
    patient: PatientIdentity | None = None
    patient_id: str | None = None
    phone: str | None = None

    content_type: MessageContentType = MessageContentType.TEXT
    # Cap inbound text length: WhatsApp itself maxes out at 4096 chars, so this
    # generous bound never rejects a legitimate message but stops an abusive /
    # malformed caller from feeding an unbounded blob into the LLM (token-cost /
    # DoS) or the DB.
    text: str | None = Field(default=None, max_length=8000)
    voice_url: str | None = None
    audio_url: HttpUrl | None = None
    audio_duration_seconds: int | None = Field(default=None, ge=0)

    # How the patient sent the inbound. ``content_type`` describes the
    # raw wire format (TEXT / AUDIO etc.); ``input_kind`` describes
    # what the orchestrator is processing — set to ``voice`` when the
    # webhook has already transcribed an audio note into ``text``, so
    # the inbox row can show a voice badge. Optional; defaults to text.
    input_kind: Literal["text", "voice", "image", "button"] | None = None

    intent: Intent | IntentType | str = Intent.UNKNOWN
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    received_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    sent_at: datetime | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self) -> "MessageIn":
        if not self.patient_id and self.patient is not None:
            self.patient_id = self.patient.patient_id

        if self.content_type == MessageContentType.TEXT and not self.text:
            raise ValueError("text is required when content_type is 'text'")
        if self.content_type == MessageContentType.AUDIO and not (self.audio_url or self.voice_url):
            raise ValueError("audio_url or voice_url is required when content_type is 'audio'")

        return self


class QuickReply(BaseModel):
    id: str
    title: str


class ActionButton(BaseModel):
    id: str
    label: str
    action: str


class ListRow(BaseModel):
    """One tappable row in a WhatsApp interactive list message.

    Meta limits: id ≤ 200 chars, title ≤ 24 chars, description ≤ 72 chars.
    The gateway truncates defensively before sending so a too-long title
    doesn't fail the whole batch.
    """

    id: str
    title: str
    description: str | None = None


class Destination(BaseModel):
    channel_type: ChannelType
    recipient_address: str


class MessageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Legacy/simple contract fields
    patient_id: str | None = None
    phone: str | None = None
    body: str | None = None
    use_template: bool = False

    # Expanded contract fields
    content: str | None = None
    mode: MessageMode | None = None
    category: MessageCategory = MessageCategory.UTILITY
    destination: Destination | None = None

    template_name: str | None = None
    template_params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    # WhatsApp template LANGUAGE code (e.g. "en", "hi", "ta"). Meta resolves the
    # matching language version of the named template. When omitted the gateway
    # falls back to WHATSAPP_TEMPLATE_LANGUAGE (default "en"). Set from the
    # patient's preferred_language so a Hindi-speaking patient gets the Hindi
    # template version (must be submitted+approved at Meta for that language).
    language: str | None = None
    quick_replies: list[QuickReply] = Field(default_factory=list)
    buttons: list[ActionButton] = Field(default_factory=list)

    # Interactive list message rows (max 10). When `list_rows` is non-empty
    # the gateway sends an interactive list (richer than reply buttons; up
    # to 10 tappable rows). `list_rows` and `buttons` are mutually exclusive
    # at the protocol level — if both present the gateway prefers list.
    list_rows: list[ListRow] = Field(default_factory=list)
    # Up to 20 chars — the trigger button shown that opens the list panel.
    # Defaults to "Select" in the gateway when omitted.
    list_button_label: str | None = None
    # Up to 24 chars — section header above the rows. Defaults to "Options".
    list_section_title: str | None = None
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @model_validator(mode="after")
    def normalize_compatibility_fields(self) -> "MessageOut":
        if self.content is None and self.body is not None:
            self.content = self.body
        if self.body is None and self.content is not None:
            self.body = self.content

        if self.mode is None:
            self.mode = MessageMode.TEMPLATE if self.use_template else MessageMode.FREEFORM
        if self.mode == MessageMode.TEMPLATE:
            self.use_template = True

        if self.mode == MessageMode.TEMPLATE and not self.template_name:
            raise ValueError("template_name is required when template mode is used")

        return self


class Event(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_type: EventType
    patient_id: str
    event_id: str = Field(default_factory=lambda: f"evt_{int(datetime.now(tz=timezone.utc).timestamp())}")
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    # Legacy scheduler compatibility
    at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBase(Event):
    correlation_id: str | None = None


class DoseDueEvent(EventBase):
    medication_name: str
    dose_instruction: str
    due_at: datetime


class DoseConfirmedEvent(EventBase):
    medication_name: str
    confirmed_at: datetime
    confirmation_channel: ChannelType


class DoseMissedEvent(EventBase):
    medication_name: str
    scheduled_at: datetime
    grace_window_minutes: int = Field(ge=0)


class RefillDueEvent(EventBase):
    medication_name: str
    refill_by: datetime
    remaining_doses: int = Field(ge=0)


class TriageAlertEvent(EventBase):
    severity: Literal["low", "medium", "high", "critical"]
    reason: str
    escalation_target: str
