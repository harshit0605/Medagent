"""DB-backed adapters for the PolicyGate state stores (async).

The pure-Python ``PatientStateStore`` and ``AuditTrail`` in
:mod:`services.orchestrator.policy_gate` stay as in-memory defaults so unit
tests remain hermetic. Production code uses these adapters, which delegate
to the async repositories so policy decisions are durable + auditable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import audit as audit_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from services.orchestrator.policy_gate import (
    ALLOWED_FLOW_ACTIONS,
    ALLOWED_OUTBOUND_MODES,
    AuditTrail,
    PatientStateStore,
    PolicyDecision,
    PolicyGate,
)


class DbPatientStateStore(PatientStateStore):
    """``PatientStateStore`` backed by the ``patient_inbound_state`` table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__()
        self._session = session

    async def set_last_inbound_timestamp(
        self, patient_id: str, inbound_timestamp: datetime
    ) -> None:
        if inbound_timestamp.tzinfo is None:
            inbound_timestamp = inbound_timestamp.replace(tzinfo=timezone.utc)
        else:
            inbound_timestamp = inbound_timestamp.astimezone(timezone.utc)
        await patient_inbound_repo.set_last_inbound(
            self._session, patient_id, inbound_timestamp
        )

    async def get_last_inbound_timestamp(self, patient_id: str) -> Optional[datetime]:
        return await patient_inbound_repo.get_last_inbound(self._session, patient_id)


class DbAuditTrail(AuditTrail):
    """``AuditTrail`` that writes ``audit_records`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__()
        self._session = session

    async def log_policy_decision(self, decision: PolicyDecision) -> None:  # type: ignore[override]
        if decision.flow_action not in ALLOWED_FLOW_ACTIONS:
            raise ValueError(f"Invalid flow action: {decision.flow_action}")
        if decision.outbound_mode not in ALLOWED_OUTBOUND_MODES:
            raise ValueError(f"Invalid outbound mode: {decision.outbound_mode}")

        await audit_repo.log_policy_decision(
            self._session,
            patient_id=decision.patient_id,
            outbound_mode=decision.outbound_mode,
            flow_action=decision.flow_action,
            reason_codes=list(decision.reason_codes),
            details=dict(decision.details),
        )


def build_db_policy_gate(session: AsyncSession) -> PolicyGate:
    return PolicyGate(
        state_store=DbPatientStateStore(session),
        audit_trail=DbAuditTrail(session),
    )
