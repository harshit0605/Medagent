from __future__ import annotations

import enum
from datetime import date, datetime

from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for application models."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AdherenceStatus(enum.Enum):
    scheduled = "scheduled"
    taken = "taken"
    missed = "missed"
    skipped = "skipped"
    delayed = "delayed"


class AlertSeverity(enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class AlertLifecycle(enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    closed = "closed"


class OrderStatus(enum.Enum):
    pending = "pending"
    processing = "processing"
    shipped = "shipped"
    delivered = "delivered"
    canceled = "canceled"


class VerificationStatus(enum.Enum):
    pending = "pending"
    verified = "verified"
    rejected = "rejected"


class TemplateCategory(enum.Enum):
    utility = "utility"
    marketing = "marketing"
    transactional = "transactional"


class OpsTicketStatus(enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class FollowupStatus(enum.Enum):
    due = "due"
    booked = "booked"
    completed = "completed"
    reviewed = "reviewed"


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    consent_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Opt-out audit trail (migration 20260507_0025). Set when the
    # patient sends STOP / UNSUBSCRIBE OR ops manually flips consent.
    # Cleared when the patient opts back in via START. The actual
    # outbound gate is still ``consent_sms``; these columns just
    # record context.
    consent_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    consent_revoked_reason: Mapped[str | None] = mapped_column(String(255))

    # Ops-initiated bot pause (migration 20260507_0026). Distinct
    # from consent revocation: this is an ops emergency-brake that
    # halts outbound WITHOUT touching consent_sms or telling the
    # patient anything (no ack message). When set, the dispatcher
    # treats every scheduled event for this patient as
    # ``not_applicable:bot_paused``. Cleared by ops via the patient
    # detail UI.
    bot_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    bot_paused_reason: Mapped[str | None] = mapped_column(String(255))
    bot_paused_by: Mapped[str | None] = mapped_column(String(128))

    # Right-of-erasure marker (migration 20260507_0028). When set,
    # the row's PII has been overwritten irreversibly per GDPR
    # Art. 17 / India DPDP §13. Clinical data referenced via FK
    # is retained for medical retention but is no longer tied to
    # a real person.
    erased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    cohort_fall_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cohort_diabetes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cohort_cardiac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cohort_asthma: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cohort_post_op: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cohort_pregnancy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Multi-patient household membership (nullable — most patients are
    # standalone). SET NULL on household delete.
    household_id: Mapped[int | None] = mapped_column(
        ForeignKey("households.id", ondelete="SET NULL")
    )

    # ISO-639-1 (with optional region) — e.g. ``en``, ``hi``, ``ta``,
    # ``en-IN``. Drives LLM-generated outbound copy (recaps, free-form
    # replies). Validated against an allowlist at the orchestrator
    # endpoint; the column itself is permissive so we can extend the
    # allowlist without a migration.
    preferred_language: Mapped[str] = mapped_column(
        String(8), nullable=False, default="en"
    )

    # Drives the onboarding state machine — see
    # ``services/orchestrator/onboarding_handler.py``. NULL ≈ legacy /
    # already-onboarded; ``done`` is the terminal state. Other values:
    # ``pending``, ``needs_name``, ``needs_cohorts``, ``needs_consent``.
    onboarding_step: Mapped[str | None] = mapped_column(String(32))

    # Consecutive re-prompt count at the current ``onboarding_step``.
    # Bumped on every invalid input; reset to 0 on state advance. After
    # ``ESCALATION_THRESHOLD`` (3) failures the handler opens an
    # ``onboarding_stuck`` ops ticket so a teammate can intervene.
    onboarding_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # Timestamp of the last ``onboarding_step`` transition. NULL for
    # legacy rows (no transition recorded yet). The handler treats a
    # transition older than ``STALE_AFTER_DAYS`` as "patient ghosted"
    # and resets them to ``pending`` on next inbound.
    onboarding_step_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    caregivers: Mapped[list[Caregiver]] = relationship(back_populates="patient")
    prescriptions: Mapped[list[Prescription]] = relationship(back_populates="patient")
    regimens: Mapped[list[Regimen]] = relationship(back_populates="patient")
    adherence_events: Mapped[list[AdherenceEvent]] = relationship(back_populates="patient")
    alerts: Mapped[list[Alert]] = relationship(back_populates="patient")
    orders: Mapped[list[Order]] = relationship(back_populates="patient")
    pregnancies: Mapped[list[Pregnancy]] = relationship(back_populates="patient")
    post_op_episodes: Mapped[list[PostOpEpisode]] = relationship(
        back_populates="patient"
    )
    household: Mapped[Household | None] = relationship(
        back_populates="members"
    )


class Caregiver(TimestampMixin, Base):
    """Family member / care contact authorised to receive copies of
    selected patient communications. Adding a caregiver alone does NOT
    grant any access — ``consent_status`` must be ``confirmed`` AND the
    relevant per-channel flag (e.g. ``notify_on_recap``) must be true
    before any fan-out happens.

    ``consent_status`` lifecycle: pending → confirmed (active) /
    declined (won't receive). ``revoked`` is terminal — caregiver
    previously consented, then explicitly asked to stop. Use ``active``
    (soft-delete) to remove from the patient-detail UI list while
    preserving the audit trail.
    """

    __tablename__ = "caregivers"
    __table_args__ = (
        Index("ix_caregivers_patient_active", "patient_id", "active"),
        Index("ix_caregivers_consent_status", "consent_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_to_patient: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(32), nullable=False)

    permission_view_phi: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permission_manage_medications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permission_manage_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    consent_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    consent_confirmed_by: Mapped[str | None] = mapped_column(String(128))
    notify_on_recap: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    patient: Mapped[Patient] = relationship(back_populates="caregivers")


class Prescription(TimestampMixin, Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    source_upload_url: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    human_verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status"),
        nullable=False,
        default=VerificationStatus.pending,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[str | None] = mapped_column(String(255))

    patient: Mapped[Patient] = relationship(back_populates="prescriptions")
    regimens: Mapped[list[Regimen]] = relationship(back_populates="prescription")


class Regimen(TimestampMixin, Base):
    __tablename__ = "regimens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    prescription_id: Mapped[int | None] = mapped_column(ForeignKey("prescriptions.id", ondelete="SET NULL"))

    medication_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dose: Mapped[str] = mapped_column(String(128), nullable=False)
    schedule: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    strict_timing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)

    # Refill tracking (optional). When both are set, the refill reminder
    # materializer schedules T-7/T-3/T-1 reminders before
    # (supply_started_on + supply_days_initial). The Refilled button tap
    # resets supply_started_on to today so the next cycle materializes.
    supply_days_initial: Mapped[int | None] = mapped_column(Integer)
    supply_started_on: Mapped[date | None] = mapped_column(Date)

    patient: Mapped[Patient] = relationship(back_populates="regimens")
    prescription: Mapped[Prescription | None] = relationship(back_populates="regimens")
    adherence_events: Mapped[list[AdherenceEvent]] = relationship(back_populates="regimen")


class AdherenceEvent(TimestampMixin, Base):
    __tablename__ = "adherence_events"
    __table_args__ = (
        Index("ix_adherence_events_patient_id_scheduled_at", "patient_id", "scheduled_at"),
        UniqueConstraint(
            "regimen_id", "scheduled_at", name="uq_adherence_regimen_scheduled"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    regimen_id: Mapped[int | None] = mapped_column(ForeignKey("regimens.id", ondelete="SET NULL"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AdherenceStatus] = mapped_column(
        Enum(AdherenceStatus, name="adherence_status"), nullable=False, default=AdherenceStatus.scheduled
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    channel_message_id: Mapped[str | None] = mapped_column(String(128))

    patient: Mapped[Patient] = relationship(back_populates="adherence_events")
    regimen: Mapped[Regimen | None] = relationship(back_populates="adherence_events")


class Alert(TimestampMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_patient_opened_closed", "patient_id", "opened_at", "closed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    adherence_event_id: Mapped[int | None] = mapped_column(ForeignKey("adherence_events.id", ondelete="SET NULL"))
    severity: Mapped[AlertSeverity] = mapped_column(Enum(AlertSeverity, name="alert_severity"), nullable=False)
    lifecycle_status: Mapped[AlertLifecycle] = mapped_column(
        Enum(AlertLifecycle, name="alert_lifecycle"), nullable=False, default=AlertLifecycle.open
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignee: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)

    patient: Mapped[Patient] = relationship(back_populates="alerts")


class Order(TimestampMixin, Base):
    """A medication reorder routed to a fulfillment partner.

    Created when a patient taps "Reorder" on a refill reminder (or ops/a
    partner places one on their behalf). A replaceable pharmacy adapter
    assigns ``partner`` + an optional ``partner_deeplink`` / ``partner_order_id``.
    ``status`` walks the :class:`OrderStatus` values (stored as a plain String
    following the recent convention). ``medication_name`` / ``dose`` are
    snapshotted so the order stands on its own even if the regimen changes or
    is deleted (regimen_id is SET NULL on delete).

    The ``substitution_*`` columns back the ``substitution_approval_v1`` flow:
    a partner proposes an alternative, we ask the patient to approve, and
    record the outcome here.
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_patient_status", "patient_id", "status"),
        Index("ix_orders_regimen_status", "regimen_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    regimen_id: Mapped[int | None] = mapped_column(
        ForeignKey("regimens.id", ondelete="SET NULL")
    )
    medication_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dose: Mapped[str | None] = mapped_column(String(128))
    quantity: Mapped[str | None] = mapped_column(String(64))
    partner: Mapped[str] = mapped_column(
        String(128), nullable=False, default="manual", server_default="manual"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OrderStatus.pending.value,
        server_default="pending",
    )
    partner_order_id: Mapped[str | None] = mapped_column(String(128))
    partner_deeplink: Mapped[str | None] = mapped_column(Text)
    receipt_url: Mapped[str | None] = mapped_column(Text)
    substitution_status: Mapped[str | None] = mapped_column(String(32))
    substitution_medication: Mapped[str | None] = mapped_column(String(255))
    substitution_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    requested_via: Mapped[str | None] = mapped_column(String(32))

    patient: Mapped[Patient] = relationship(back_populates="orders")


class Template(TimestampMixin, Base):
    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("name", "language", name="uq_templates_name_language"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    category: Mapped[TemplateCategory] = mapped_column(
        Enum(TemplateCategory, name="template_category"), nullable=False, default=TemplateCategory.utility
    )



class LabFollowup(TimestampMixin, Base):
    __tablename__ = "lab_followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[FollowupStatus] = mapped_column(
        Enum(FollowupStatus, name="followup_status"), nullable=False, default=FollowupStatus.due
    )
    # When this lab is supposed to happen by. Reminder materializer fires
    # at T-7d, T-1d, and T+2d (overdue) relative to this. Nullable so
    # legacy rows without a target date stay valid (skipped by materializer).
    due_by: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppointmentFollowup(TimestampMixin, Base):
    __tablename__ = "appointment_followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    clinician_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[FollowupStatus] = mapped_column(
        Enum(FollowupStatus, name="followup_status"), nullable=False, default=FollowupStatus.due
    )
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpsTicket(TimestampMixin, Base):
    __tablename__ = "ops_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # patient_id is a free-form runtime identifier (no FK to patients yet).
    patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    status: Mapped[OpsTicketStatus] = mapped_column(
        Enum(OpsTicketStatus, name="ops_ticket_status"), nullable=False, default=OpsTicketStatus.open
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    # Assignment + snooze (added in migration 20260503_0012). When
    # ``snoozed_until`` is in the future the ticket is hidden from the
    # active queue but the underlying status stays unchanged — once the
    # timestamp passes (or is cleared) the ticket comes back.
    assigned_to: Mapped[str | None] = mapped_column(String(128))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # First-cross SLA breach timestamp (migration 20260507_0023). The
    # ``is_overdue`` DTO field is a live boolean that re-evaluates
    # every request; this column is the persistent first-cross signal
    # the breach sweep stamps. Stays set after resolution so analytics
    # can count breaches; cleared on ``reopen()`` so a reopened ticket
    # starts a fresh SLA window.
    sla_breached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


# --- Runtime state tables (introduced in migration 20260429_0003) -----------
# These tables store mutable in-process state previously held in
# medagent.InMemoryStore, services.whatsapp_gateway.MESSAGE_LOG and
# services.orchestrator.policy_gate. They use string patient_id (the WhatsApp
# identifier) and intentionally have no foreign key to `patients.id` because
# the runtime flow does not yet require a registered Patient row.


class MessageDirection(enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class MessagePayloadKind(enum.Enum):
    template = "template"
    freeform = "freeform"


class TriageSeverity(enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class MissReason(enum.Enum):
    forgot = "forgot"
    side_effect = "side_effect"
    out_of_stock = "out_of_stock"
    confused = "confused"
    cost = "cost"
    other = "other"


class MissRecoveryAction(enum.Enum):
    reschedule = "reschedule"
    escalate_clinician = "escalate_clinician"
    refill_support = "refill_support"
    human_review = "human_review"


class MessageLog(Base):
    __tablename__ = "message_log"
    __table_args__ = (
        Index("ix_message_log_direction_occurred_at", "direction", "occurred_at"),
        Index("ix_message_log_patient_id_occurred_at", "patient_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction"), nullable=False
    )
    patient_id: Mapped[str | None] = mapped_column(String(128))
    payload_kind: Mapped[MessagePayloadKind | None] = mapped_column(
        Enum(MessagePayloadKind, name="message_payload_kind")
    )
    message: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Meta's per-message identifier (only set on outbound rows where
    # the send was accepted by Meta). Joins to
    # ``whatsapp_message_statuses.wamid`` — that table holds the
    # delivery / read / failure events for this message. NULL for
    # inbound rows and for outbound sends that failed before Meta
    # replied with a wamid. Partial index in migration 20260507_0024.
    wamid: Mapped[str | None] = mapped_column(String(255))


class AuditRecord(Base):
    __tablename__ = "audit_records"
    __table_args__ = (
        Index("ix_audit_records_patient_logged", "patient_id", "logged_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outbound_mode: Mapped[str | None] = mapped_column(String(16))
    flow_action: Mapped[str | None] = mapped_column(String(16))
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InboundClassification(Base):
    """Doctor-inbox row — one per freeform inbound (action-tap messages
    are not classified, they're already structured).

    Filled by the orchestrator's ``/route`` endpoint AFTER the workflow
    runs, so the row carries everything the inbox UI needs to render a
    quick triage view: category + 1-line summary + urgency for skim,
    plus the handler that answered and whether the case escalated to
    ops (with the ticket id for click-through).

    ``category`` enum (string, validated at write time):
      clinical_question, administrative, billing, scheduling, faq,
      social, unsafe, action_tap, unknown
    ``urgency`` enum: critical | high | medium | low
    """

    __tablename__ = "inbound_classifications"
    __table_args__ = (
        Index(
            "ix_inbound_classifications_patient",
            "patient_phone",
            "created_at",
        ),
        Index(
            "ix_inbound_classifications_category", "category", "created_at"
        ),
        Index(
            "ix_inbound_classifications_urgency", "urgency", "created_at"
        ),
        Index(
            "ix_inbound_classifications_escalated",
            "escalated",
            "created_at",
        ),
        Index("ix_inbound_classifications_created_at", "created_at"),
        Index(
            "ix_inbound_classifications_input_kind",
            "input_kind",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str | None] = mapped_column(String(128))
    patient_phone: Mapped[str] = mapped_column(String(128), nullable=False)
    patient_db_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL")
    )
    inbound_text: Mapped[str | None] = mapped_column(Text)
    input_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text"
    )
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )
    summary: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(
        String(16), nullable=False, default="low"
    )
    handler_used: Mapped[str | None] = mapped_column(String(64))
    response_text: Mapped[str | None] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("ops_tickets.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Bot-reply quality feedback (migration 20260507_0029).
    # +1 = thumbs-up, -1 = thumbs-down, NULL = no feedback yet.
    # Drives model-regression detection: queryable signal for
    # "how often does the bot get category X wrong this week?".
    feedback_rating: Mapped[int | None] = mapped_column(SmallInteger)
    feedback_note: Mapped[str | None] = mapped_column(Text)
    feedback_by: Mapped[str | None] = mapped_column(String(128))
    feedback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # End-to-end /route duration (migration 20260508_0031). The
    # time from inbound arrival → response built, in milliseconds.
    # Distinct from per-LLM-call latency in llm_call_logs — this
    # captures the full path including DB writes + handler
    # dispatch + multiple chained LLM calls.
    request_duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Triage classifier verdict (migration 20260509_0036). NULL
    # when the message didn't trip the threshold (low/medium/none
    # severity) or the triage classifier didn't run. Drives the
    # inbox-row severity badge so doctors scanning inbox can spot
    # alert-tied messages without bouncing to /clinical-alerts.
    clinical_severity: Mapped[str | None] = mapped_column(String(16))


class LlmCallLog(Base):
    """One row per OpenAI API call. Powers cost + latency
    analytics across the bot's many LLM call sites (compose,
    intent detection, inbox classification, recap generation,
    language detection, prescription vision, booking-agent ReAct
    loop).

    Columns capture the context (patient + message + call_kind)
    so analytics can slice by handler ("how much do recap
    generations cost per month?") and by patient ("which patient
    is the most LLM-expensive?"). The ``cost_usd_micros`` is
    computed at ingest time from a per-model rate table — stored
    as integer 10⁻⁶ USD so summing over millions of rows doesn't
    drift due to floating-point.

    Migration: 20260508_0031.
    """

    __tablename__ = "llm_call_logs"
    __table_args__ = (
        Index("ix_llm_call_logs_occurred_at", "occurred_at"),
        Index(
            "ix_llm_call_logs_call_kind_occurred",
            "call_kind",
            "occurred_at",
        ),
        Index(
            "ix_llm_call_logs_patient_occurred",
            "patient_id",
            "occurred_at",
            postgresql_where=text("patient_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # ``patient_id`` mirrors the convention used by ops_tickets +
    # audit_records: the WhatsApp phone (string), nullable for
    # platform calls (e.g. delivery alert sweeps that classify
    # without a patient context).
    patient_id: Mapped[str | None] = mapped_column(String(128))
    # The originating message_id when the call is part of an
    # inbound-handling flow. Joins back to inbound_classifications
    # for end-to-end "this inbound cost $0.012" attribution.
    message_id: Mapped[str | None] = mapped_column(String(128))
    # Which LLM-using code path made this call. Free-form string
    # — the analytics endpoint groups by it.
    call_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd_micros: Mapped[int] = mapped_column(
        # BigInteger because (very-busy)x(many months)x(GPT-4
        # rates) can exceed INT range when summed.
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(String(255))


class BroadcastCampaign(Base):
    """Cohort-targeted bulk template send. Materialises into N
    ``scheduled_events`` (one per recipient) so the existing
    dispatcher path carries each individual send through retry
    policy + consent gates + delivery tracking.

    ``cohort_filter`` JSON shape (v1):

        {"cohort": "diabetes" | "cardiac" | "fall_risk"}

    Future v2 supports cohort_tag_id + composite filters.

    Status lifecycle:

        draft → materialised → completed

    On materialisation we resolve the recipient list, create the
    per-recipient ``broadcast_sends`` rows, and enqueue the
    scheduled events. ``completed`` is set when every send has
    settled (dispatched / skipped / failed) — populated by the
    detail endpoint when it sums per-status counts.

    Deliberately NOT using ``TimestampMixin``: campaigns are
    write-once for ops accounting (the per-recipient rows in
    ``broadcast_sends`` are where the action happens), so an
    ``updated_at`` would just be noise. ``created_at`` is enough.
    """

    __tablename__ = "broadcast_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_name: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    template_params: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    cohort_filter: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft"
    )
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    materialised_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    total_recipients: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text)


class BroadcastSend(Base):
    """Per-recipient row for a campaign. ``status`` is independent
    of the underlying ``scheduled_events`` row's status — the
    broadcast layer captures whether the patient was eligible
    (gate decisions BEFORE the dispatcher), and the linked
    ``scheduled_event_id`` carries the actual delivery outcome
    when status=pending → dispatched.

    skip_reason values (when ``status='skipped'``):
        - opted_out: patient consent_sms is False
        - paused: bot_paused_at is set
        - erased: patient row has been GDPR-erased
        - no_phone: patient has no phone column populated

    The (campaign_id, patient_db_id) combo isn't a unique
    constraint by design — re-running a campaign for a single
    patient (manual replay after fixing data) is occasionally
    legitimate ops work.
    """

    __tablename__ = "broadcast_sends"
    __table_args__ = (
        Index(
            "ix_broadcast_sends_campaign_status",
            "campaign_id",
            "status",
        ),
        Index(
            "ix_broadcast_sends_patient_campaign",
            "patient_db_id",
            "campaign_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey(
            "broadcast_campaigns.id", ondelete="CASCADE"
        ),
        nullable=False,
    )
    patient_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    patient_db_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    scheduled_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class VisitBrief(Base):
    """LLM-generated pre-visit summary. The doctor reads it in
    the 30 seconds before they see the patient.

    Each generation produces a fresh row — we never overwrite,
    so the audit trail shows ``compared to last week's brief,
    this patient...``. Status lifecycle:

        draft → sent → superseded

    ``draft`` is the default until the brief has been served to
    a doctor at least once (UI marks it on first GET).
    ``superseded`` is set when a newer brief is generated for
    the same ``appointment_id`` — keeps the per-appointment
    "current brief" lookup deterministic.

    ``key_metrics`` is the at-a-glance numeric layer the UI
    shows above the prose:

        {"adherence_rate": 0.83,
         "missed_doses": 4,
         "side_effect_count": 1,
         "messages_count": 12}

    ``talking_points`` and ``red_flags`` are list[str] — short
    actionable bullets the doctor can scan. Red flags carry
    explicit safety weight; talking points are softer
    suggestions ("ask about evening dose timing").

    The optional ``error`` column captures LLM-call failures so
    a failed generation still leaves a row (operator can retry)
    rather than silently disappearing.
    """

    __tablename__ = "visit_briefs"
    __table_args__ = (
        Index("ix_visit_briefs_patient_generated", "patient_id", "generated_at"),
        Index("ix_visit_briefs_appointment", "appointment_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    doctor_id: Mapped[int | None] = mapped_column(
        ForeignKey("doctors.id", ondelete="SET NULL")
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    llm_model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    talking_points: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    red_flags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    key_metrics: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft"
    )
    generated_by: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)


class ClinicalAlert(Base):
    """Patient-safety alert for clinically urgent inbound
    messages. Created by the triage classifier when severity
    crosses the ``high`` / ``critical`` threshold.

    Lifecycle:
        open → acknowledged → resolved

    Why distinct from ops_tickets and inbound_classifications:

        - ``ops_tickets`` is operator workflow (paperwork,
          billing follow-ups). Alerts are clinical signals.
        - ``inbound_classifications`` covers EVERY freeform
          message — the inbox skim view. Alerts are the small
          subset that need a doctor's eyes within minutes.

    The ``inbound_text`` snapshot decouples this row from
    ``message_log`` so the alert survives later message-log
    retention / right-of-erasure trims.
    """

    __tablename__ = "clinical_alerts"
    __table_args__ = (
        Index(
            "ix_clinical_alerts_status_severity_created",
            "status",
            "severity",
            "created_at",
        ),
        Index("ix_clinical_alerts_patient", "patient_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    patient_phone: Mapped[str | None] = mapped_column(String(128))
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_log.id", ondelete="SET NULL")
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    red_flags: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    clinical_summary: Mapped[str] = mapped_column(Text, nullable=False)
    inbound_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open"
    )
    llm_model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    # Paging audit. Updated by the pager service on every
    # attempt; ``paged_attempts`` is the cap key for the
    # re-page sweep (stop after 3 attempts so a missed page
    # doesn't become a 24h annoyance).
    paged_doctor_id: Mapped[int | None] = mapped_column(
        ForeignKey("doctors.id", ondelete="SET NULL")
    )
    paged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    paged_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )


class CarePlanGoal(Base):
    """Quantitative goal a doctor sets for a specific patient.

    Distinct from ``CarePlan`` (the cohort-level template
    that drives nudges) — goals are per-patient targets the
    doctor tracks numerically: ``HbA1c < 7.0%``,
    ``BP_systolic < 130 mmHg``, ``weight < 80 kg``.

    Lifecycle:
        active → achieved → inactive (manual or auto-on
        target-met for N consecutive observations — auto-
        achievement is a future slice; v1 ships manual
        transitions only).

    Why not enum-constrain ``metric_key``? Clinical practice
    expands the useful metric set faster than migrations
    ship — we'd be chasing the column. The label is set
    free-form at creation so each goal carries the doctor's
    chosen wording.
    """

    __tablename__ = "care_plan_goals"
    __table_args__ = (
        Index(
            "ix_care_plan_goals_patient_status",
            "patient_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_label: Mapped[str] = mapped_column(String(128), nullable=False)
    target_value: Mapped[Decimal] = mapped_column(
        Numeric(precision=8, scale=3), nullable=False
    )
    comparator: Mapped[str] = mapped_column(
        String(32), nullable=False, default="less_than"
    )
    target_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    ends_on: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MetricObservation(Base):
    """A measured / reported value at a point in time.

    Optionally tied to a ``CarePlanGoal`` via ``goal_id``;
    standalone observations (one-off lab result before a
    goal exists) keep ``goal_id`` NULL. Observations
    survive goal deletion (FK is SET NULL) so chart history
    stays intact.

    ``source`` distinguishes manual ops entry vs
    patient-self-report vs lab integration vs device
    feed — drives downstream trust scoring (a clinic-lab
    HbA1c outweighs a patient self-report) once that v2
    layer ships.

    ``observed_at`` is when the measurement was taken (the
    timeline timestamp), NOT when it was recorded into our
    system. Backfilled lab results have ``observed_at`` =
    yesterday and ``created_at`` = now; the trend chart
    sorts by observed_at.
    """

    __tablename__ = "metric_observations"
    __table_args__ = (
        Index(
            "ix_metric_observations_patient_metric",
            "patient_id",
            "metric_key",
            "observed_at",
        ),
        Index(
            "ix_metric_observations_goal",
            "goal_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("care_plan_goals.id", ondelete="SET NULL")
    )
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual"
    )
    recorded_by: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ServiceHeartbeat(Base):
    """One row per scheduler/orchestrator loop. Each loop upserts at
    the end of every pass — successful or failed — so the /ops/health
    page can show "alive and processing" vs "stale by N minutes" at a
    glance, plus per-loop counters from the most recent pass.

    Component naming convention: ``<service>.<loop>`` — e.g.
    ``scheduler.dispatch``, ``scheduler.recap_sweep``. Single row per
    component (PK is component name); upsert semantics in the repo.
    """

    __tablename__ = "service_heartbeats"

    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ok"
    )
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    consecutive_errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PatientInboundState(Base):
    __tablename__ = "patient_inbound_state"

    patient_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_inbound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HumanQueueItem(Base):
    __tablename__ = "human_queue_items"
    __table_args__ = (
        Index(
            "ix_human_queue_patient_med_reason",
            "patient_id",
            "medication",
            "reason",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    medication: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="p2")
    sla_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)


class MissRecoveryEvent(Base):
    __tablename__ = "miss_recovery_events"
    __table_args__ = (
        Index("ix_miss_recovery_patient_occurred", "patient_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    medication: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[MissReason] = mapped_column(Enum(MissReason, name="miss_reason"), nullable=False)
    action: Mapped[MissRecoveryAction] = mapped_column(
        Enum(MissRecoveryAction, name="miss_recovery_action"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TriageDecisionRecord(Base):
    __tablename__ = "triage_decisions"
    __table_args__ = (
        Index("ix_triage_patient_occurred", "patient_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[TriageSeverity] = mapped_column(
        Enum(TriageSeverity, name="triage_severity"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    escalation_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScheduledEventStatus(enum.Enum):
    pending = "pending"
    dispatched = "dispatched"
    skipped = "skipped"
    failed = "failed"


class DoctorOAuthStatus(enum.Enum):
    disconnected = "disconnected"
    connected = "connected"
    expired = "expired"
    revoked = "revoked"


class Doctor(TimestampMixin, Base):
    """Provider with their own Google Calendar.

    Per-doctor OAuth: each doctor authorizes us via the Next.js OAuth flow,
    we persist their refresh token encrypted-at-rest (Fernet, key =
    ``MEDAGENT_FERNET_KEY``). Bookings land directly on their primary
    calendar with the patient added as a guest.
    """

    __tablename__ = "doctors"
    __table_args__ = (Index("ix_doctors_email", "email", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False, default="primary")
    # Critical-alert paging fanout includes this doctor when
    # the patient's primary doctor (most recent appointment)
    # can't be resolved. Admin-toggled via the doctors page.
    is_on_call: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    oauth_status: Mapped[DoctorOAuthStatus] = mapped_column(
        Enum(DoctorOAuthStatus, name="doctor_oauth_status"),
        nullable=False,
        default=DoctorOAuthStatus.disconnected,
    )
    # Fernet-encrypted; never store plaintext.
    oauth_refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    # Cached access token (short-lived; we refresh on demand). Plaintext OK since
    # it expires in ~1h and is bound to this single calendar's scopes.
    oauth_access_token: Mapped[str | None] = mapped_column(Text)
    oauth_access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oauth_scopes: Mapped[str | None] = mapped_column(Text)
    # Google's stable user identifier (the `sub` claim) — useful for spotting
    # if a doctor accidentally re-authorized with the wrong account.
    google_user_id: Mapped[str | None] = mapped_column(String(64))

    # Inbound calendar-sync state (migration 20260508_0030). Powers
    # the calendar_sync_sweep — Google's events.list API returns a
    # ``nextSyncToken`` we present on the next call to get only
    # changed events. NULL = never synced; the sweep does a full
    # initial sync to seed the token.
    gcal_sync_token: Mapped[str | None] = mapped_column(Text)
    gcal_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class AppointmentStatus(enum.Enum):
    proposed = "proposed"        # bot offered the slot, patient hasn't confirmed
    confirmed = "confirmed"      # booked, calendar event created
    cancelled = "cancelled"
    completed = "completed"      # past appointment, attended
    no_show = "no_show"


class Appointment(TimestampMixin, Base):
    """A scheduled appointment between a patient and a doctor.

    Mirrors the Google Calendar event when ``calendar_event_id`` is set;
    we keep our own row so the agent / ops console can list a patient's
    appointments without round-tripping to Calendar, and so we have a
    durable audit trail independent of the doctor's calendar permissions.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_patient", "patient_id"),
        Index("ix_appointments_doctor_scheduled", "doctor_id", "scheduled_for"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"),
        nullable=False,
        default=AppointmentStatus.confirmed,
    )
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))
    calendar_html_link: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="whatsapp_booking_agent"
    )
    summary: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)


class RecapStatus(enum.Enum):
    draft = "draft"
    sent = "sent"
    acknowledged = "acknowledged"
    questioned = "questioned"  # patient tapped "I have a question"


class AppointmentRecap(TimestampMixin, Base):
    """Doctor-authored, LLM-polished after-visit recap sent to the patient
    over WhatsApp. One per appointment (uniqueness enforced).

    ``structured_payload`` is the doctor-filled JSON that drives generation:
    {
      "meds_added":   [{"regimen_id": int, "name": str, "instructions": str}],
      "meds_changed": [{"regimen_id": int, "name": str, "change": str}],
      "meds_stopped": [{"regimen_id": int, "name": str}],
      "labs_ordered": [{"lab_followup_id": int, "test_name": str}],
      "next_followup_in_days": int | null,
      "red_flags":    [str, ...]
    }
    """

    __tablename__ = "appointment_recaps"
    __table_args__ = (
        UniqueConstraint("appointment_id", name="uq_appointment_recaps_appointment"),
        Index("ix_appointment_recaps_patient", "patient_id"),
        Index("ix_appointment_recaps_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )
    doctor_notes: Mapped[str | None] = mapped_column(Text)
    structured_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generated_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RecapStatus] = mapped_column(
        Enum(RecapStatus, name="recap_status"),
        nullable=False,
        default=RecapStatus.draft,
    )
    sent_message_id: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authored_by: Mapped[str | None] = mapped_column(String(128))


class CarePlanExemption(TimestampMixin, Base):
    """A patient-level opt-out from a specific standing-order care plan.

    Active when ``revoked_at IS NULL`` AND (``expires_at IS NULL`` or
    ``expires_at > now()``). The sweep skips any patient with an active
    exemption for the plan being swept. Historical rows (revoked or
    expired) are kept for audit so a clinician can see why a patient
    was once exempted.

    No uniqueness on ``(patient_id, care_plan_id)`` — a patient can be
    exempted, then re-included, then exempted again. The repo enforces
    "only one *active* exemption at a time" in code.
    """

    __tablename__ = "care_plan_exemptions"
    __table_args__ = (
        Index("ix_care_plan_exemptions_patient", "patient_id"),
        Index("ix_care_plan_exemptions_plan", "care_plan_id"),
        Index(
            "ix_care_plan_exemptions_active",
            "patient_id",
            "care_plan_id",
            "revoked_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    care_plan_id: Mapped[int] = mapped_column(
        ForeignKey("care_plans.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(128))
    revoked_by: Mapped[str | None] = mapped_column(String(128))


class CohortTag(TimestampMixin, Base):
    """Clinician-authored cohort label, e.g. "Post-MI" or "Pregnancy 3T".
    Slug is the URL-safe stable identifier; label is the display name.
    Patients are assigned via the ``patient_cohort_tags`` M2M; care
    plans target tags via ``CarePlan.cohort_tag_id``.

    Adding tags is intentionally low-friction (no migration needed) —
    that's the structural unlock over the legacy boolean cohort columns
    on Patient, which require a column-add migration to extend.
    """

    __tablename__ = "cohort_tags"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_cohort_tags_slug"),
        Index("ix_cohort_tags_active", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))


class PatientCohortTag(Base):
    """Assignment of a clinician-authored cohort tag to a patient.

    Uniqueness on (patient_id, cohort_tag_id) so a patient can't be
    assigned to the same tag twice. Removing an assignment is a hard
    delete (no soft-delete here; the audit trail lives on the
    ``assigned_at`` timestamp + the ``assigned_by`` actor — and a
    re-assignment after removal creates a fresh row with a new
    timestamp, which is what we want for clinical history)."""

    __tablename__ = "patient_cohort_tags"
    __table_args__ = (
        UniqueConstraint(
            "patient_id",
            "cohort_tag_id",
            name="uq_patient_cohort_tags_assignment",
        ),
        Index("ix_patient_cohort_tags_patient", "patient_id"),
        Index("ix_patient_cohort_tags_tag", "cohort_tag_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    cohort_tag_id: Mapped[int] = mapped_column(
        ForeignKey("cohort_tags.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by: Mapped[str | None] = mapped_column(String(128))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CarePlan(TimestampMixin, Base):
    """One standing-order care rule — "patients in cohort X get
    test_name Y every cadence_days days." Editable from the ops console
    so a clinician can add (e.g.) "all post-MI → lipid panel every 6
    months" without a deploy.

    A plan targets EXACTLY ONE cohort dimension:
      - ``cohort_attr`` (legacy boolean column on Patient — diabetes /
        cardiac / fall_risk), validated at the application layer
        against the ``KNOWN_COHORT_ATTRS`` allowlist.
      - ``cohort_tag_id`` (clinician-authored tag) — points at a row in
        ``cohort_tags``; patients are members via the
        ``patient_cohort_tags`` M2M.

    Exactly one of the two is set; the orchestrator endpoints enforce
    the XOR. Both are nullable in the schema so we can add new cohort
    paths in the future without column changes.
    """

    __tablename__ = "care_plans"
    __table_args__ = (
        UniqueConstraint(
            "cohort_attr", "test_name", name="uq_care_plans_cohort_test"
        ),
        Index("ix_care_plans_active", "active"),
        Index("ix_care_plans_cohort_tag_id", "cohort_tag_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_attr: Mapped[str | None] = mapped_column(String(64))
    cohort_tag_id: Mapped[int | None] = mapped_column(
        ForeignKey("cohort_tags.id", ondelete="RESTRICT")
    )
    test_name: Mapped[str] = mapped_column(String(255), nullable=False)
    cadence_days: Mapped[int] = mapped_column(Integer, nullable=False)
    due_in_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))


class WhatsAppMessageStatus(Base):
    """Delivery / read receipts for outbound WhatsApp messages.

    Meta delivers status updates (sent / delivered / read / failed) on the
    same webhook as inbound messages. The Next.js webhook route parses them
    out of the raw body and POSTs each one to the gateway's
    ``/internal/whatsapp/status`` endpoint, which upserts here keyed by
    ``wamid`` (one row per outbound message id; status progresses over time).
    """

    __tablename__ = "whatsapp_message_statuses"
    __table_args__ = (
        Index("ix_whatsapp_status_recipient", "recipient_id"),
        Index("ix_whatsapp_status_updated_at", "updated_at"),
    )

    wamid: Mapped[str] = mapped_column(String(255), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_id: Mapped[str | None] = mapped_column(String(128))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_title: Mapped[str | None] = mapped_column(String(255))
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ScheduledEvent(Base):
    """Persistent queue of one-shot system events to deliver at a scheduled time.

    Inserted by ``services.scheduler`` (HTTP /emit-* or external triggers); a
    polling loop in the same service claims rows where ``scheduled_for <= now()``,
    dispatches them to the WhatsApp gateway, then marks the status accordingly.
    """

    __tablename__ = "scheduled_events"
    __table_args__ = (
        Index("ix_scheduled_events_status_scheduled_for", "status", "scheduled_for"),
        Index("ix_scheduled_events_patient", "patient_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[ScheduledEventStatus] = mapped_column(
        Enum(ScheduledEventStatus, name="scheduled_event_status"),
        nullable=False,
        default=ScheduledEventStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    # Retry policy + dead-letter queue (migration 20260507_0027).
    # ``attempt_count`` increments on every dispatch failure. When
    # ``next_retry_at`` is non-null and in the future the row is
    # waiting for another attempt; when null AND status is failed
    # the row is in the dead-letter queue (attempts exhausted).
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class PregnancyStatus(enum.Enum):
    """Lifecycle of a pregnancy episode. Stored as a plain String on the
    row (see :class:`Pregnancy`) — these constants are the canonical values."""

    active = "active"
    ended = "ended"


class Pregnancy(TimestampMixin, Base):
    """A single pregnancy episode for a patient, anchored on the LMP.

    The pregnancy timeline engine reads ``lmp_date`` (or derives it from
    ``edd``) to compute gestational age and materialize trimester-appropriate
    milestone reminders (ANC visits, labs, scans, supplements) plus a weekly
    check-in. The milestone sweep walks ``status == 'active'`` rows.

    Either ``lmp_date`` or ``edd`` must be set; the engine fills the missing
    one via Naegele's rule (EDD = LMP + 280 days). ``status`` is a plain
    String (not a PG enum) following the care_plan_goals convention; use the
    :class:`PregnancyStatus` constants for the values.

    A partial unique index (migration 20260510_0038) enforces at most one
    ``active`` row per patient while keeping historical ``ended`` rows.
    """

    __tablename__ = "pregnancies"
    __table_args__ = (
        Index("ix_pregnancies_patient_status", "patient_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    lmp_date: Mapped[date | None] = mapped_column(Date)
    edd: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PregnancyStatus.active.value
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    patient: Mapped[Patient] = relationship(back_populates="pregnancies")


class AsthmaTriggerLog(Base):
    """A single asthma trigger the patient logged ("dust", "exercise",
    "cold air"). Powers the trigger diary / voice trigger diary (SoT §3C) so
    the clinic can spot patterns. Categorical free-text — distinct from the
    numeric ``metric_observations`` used for rescue-inhaler puff counts.

    ``logged_at`` is when the patient reported it (defaults to insert time);
    ``source`` distinguishes patient_self_report vs voice vs manual entry.
    """

    __tablename__ = "asthma_trigger_logs"
    __table_args__ = (
        Index(
            "ix_asthma_trigger_logs_patient_logged", "patient_id", "logged_at"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    trigger_text: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="patient_self_report"
    )
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PostOpEpisode(TimestampMixin, Base):
    """A post-surgical recovery episode, anchored on the surgery date.

    Drives day-N checklist reminders (wound check, suture removal, follow-up)
    plus a wound-photo review prompt. ``status`` (active | ended) is a plain
    String; a partial unique index (migration 0041) enforces one active
    episode per patient.
    """

    __tablename__ = "post_op_episodes"
    __table_args__ = (
        Index(
            "ix_post_op_episodes_patient_status", "patient_id", "status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    procedure_name: Mapped[str] = mapped_column(String(255), nullable=False)
    surgery_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    patient: Mapped[Patient] = relationship(
        back_populates="post_op_episodes"
    )


class Household(TimestampMixin, Base):
    """A multi-patient household. Groups several patients (members) under one
    household so a caregiver can oversee 1→many. ``primary_caregiver_phone``
    is the household-level point of contact."""

    __tablename__ = "households"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_caregiver_phone: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    members: Mapped[list[Patient]] = relationship(
        back_populates="household"
    )
