from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from pydantic import BaseModel

from shared.contracts.models import IntentType, MessageIn, MessageOut, QuickReply

app = FastAPI(title="orchestrator")


class OrchestratorRequest(BaseModel):
    message: MessageIn
    last_user_message_at: datetime | None = None


class PolicyDecision(BaseModel):
    in_customer_service_window: bool
    use_template: bool
    reason: str


def detect_intent(text: str | None) -> IntentType:
    if not text:
        return IntentType.GENERAL_QUESTION
    lower = text.lower()
    if any(x in lower for x in ["taken", "snooze", "skip"]):
        return IntentType.ADHERENCE_UPDATE
    if "refill" in lower or "reorder" in lower:
        return IntentType.REFILL_REQUEST
    if "symptom" in lower or "breath" in lower or "pain" in lower:
        return IntentType.SYMPTOM_REPORT
    return IntentType.GENERAL_QUESTION


def policy_gate(now: datetime, last_user_message_at: datetime | None) -> PolicyDecision:
    if last_user_message_at is None:
        return PolicyDecision(
            in_customer_service_window=False,
            use_template=True,
            reason="No prior inbound message timestamp; require template send",
        )
    in_window = (now - last_user_message_at) <= timedelta(hours=24)
    return PolicyDecision(
        in_customer_service_window=in_window,
        use_template=not in_window,
        reason="Within 24h freeform allowed" if in_window else "Outside 24h template required",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/route")
def route(payload: OrchestratorRequest) -> dict:
    now = datetime.now(timezone.utc)
    intent = detect_intent(payload.message.text)
    decision = policy_gate(now=now, last_user_message_at=payload.last_user_message_at)

    body = "Got it."
    if intent == IntentType.ADHERENCE_UPDATE:
        body = "Adherence updated. Reply HELP for a pharmacist or CALL for clinician callback."
    elif intent == IntentType.REFILL_REQUEST:
        body = "Refill request captured. Reply CALL for pharmacist support."
    elif intent == IntentType.SYMPTOM_REPORT:
        body = "Thanks for sharing. We recommend clinician review. Reply CALL to connect now."

    msg = MessageOut(
        patient_id=payload.message.patient_id,
        phone=payload.message.phone,
        body=body,
        use_template=decision.use_template,
        template_name="escalate_call_v1" if decision.use_template else None,
        quick_replies=[
            QuickReply(id="call", title="CALL"),
            QuickReply(id="help", title="HELP"),
        ],
    )
    return {
        "intent": intent,
        "policy": decision,
        "message_out": msg,
    }
