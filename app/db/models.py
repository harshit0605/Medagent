from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    consent_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    cohort_fall_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cohort_diabetes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cohort_cardiac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    caregivers: Mapped[list[Caregiver]] = relationship(back_populates="patient")
    prescriptions: Mapped[list[Prescription]] = relationship(back_populates="patient")
    regimens: Mapped[list[Regimen]] = relationship(back_populates="patient")
    adherence_events: Mapped[list[AdherenceEvent]] = relationship(back_populates="patient")
    alerts: Mapped[list[Alert]] = relationship(back_populates="patient")
    orders: Mapped[list[Order]] = relationship(back_populates="patient")


class Caregiver(TimestampMixin, Base):
    __tablename__ = "caregivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_to_patient: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(32), nullable=False)

    permission_view_phi: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permission_manage_medications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permission_manage_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

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

    patient: Mapped[Patient] = relationship(back_populates="regimens")
    prescription: Mapped[Prescription | None] = relationship(back_populates="regimens")
    adherence_events: Mapped[list[AdherenceEvent]] = relationship(back_populates="regimen")


class AdherenceEvent(TimestampMixin, Base):
    __tablename__ = "adherence_events"
    __table_args__ = (
        Index("ix_adherence_events_patient_id_scheduled_at", "patient_id", "scheduled_at"),
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
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    partner: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="order_status"), nullable=False)
    partner_order_id: Mapped[str | None] = mapped_column(String(128))
    receipt_url: Mapped[str | None] = mapped_column(Text)

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
