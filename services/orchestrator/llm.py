"""LLM wrapper for the orchestrator agent workflow (async).

Design contract:
    - Every public method returns ``None`` on ANY failure (network, timeout,
      malformed output, missing API key). The caller falls back to the
      deterministic rule-based logic.
    - Severity from :py:meth:`AgentLLM.augment_safety` is bounded: the LLM may
      escalate but never lower the rule-based severity.
    - The OpenAI SDK is imported lazily so the module is importable without
      the dependency installed (e.g. in environments that disable the LLM).
"""

from __future__ import annotations

import logging
import os
from threading import Lock
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


log = logging.getLogger(__name__)

Intent = Literal[
    "adherence_update",
    "refill_request",
    "symptom_report",
    "pregnancy_checklist",
    "followup_update",
    "booking_request",
    "general_question",
]
RiskLevel = Literal["low", "medium", "high", "critical"]

_SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_INTENT_SYSTEM_PROMPT = (
    "You classify a single inbound WhatsApp message from a patient enrolled in a "
    "medication-adherence program. Return one intent label.\n\n"
    "Labels:\n"
    "- adherence_update: took/skipped/missed a dose, side effects, dosing confusion, cost.\n"
    "- refill_request: running out, reorder, refill timing.\n"
    "- symptom_report: any new physical symptom, pain, breathing trouble, bleeding.\n"
    "- pregnancy_checklist: pregnancy, trimester, prenatal questions.\n"
    "- followup_update: telling us a lab/appointment was booked, completed, or reviewed.\n"
    "- booking_request: patient wants to manage an appointment in any way — BOOK new, "
    "CANCEL existing, RESCHEDULE existing, or LIST upcoming. Triggers on phrases like "
    "'I want to book', 'schedule an appointment', 'can I see Dr X', 'need a slot tomorrow', "
    "'cancel my appointment', 'reschedule my booking', 'move my appointment', "
    "'do I have any appointments', 'what appointments do I have'. Also routes here while a "
    "booking/cancel/reschedule flow is in progress (when the patient is replying with a "
    "slot pick like '1' or 'morning' inside an active conversation).\n"
    "- general_question: anything else, including greetings.\n\n"
    "Output JSON matching the schema. Confidence is your subjective certainty."
)

_SAFETY_SYSTEM_PROMPT = (
    "You assess the clinical safety risk of a patient message. You may ESCALATE the "
    "severity above the rule-based floor but you must NEVER reduce it.\n\n"
    "Severity definitions:\n"
    "- critical: chest pain, severe bleeding, unconscious, cannot breathe, anaphylaxis, "
    "stroke-like symptoms, suicide ideation.\n"
    "- high: clinically concerning symptoms (wheezing, hypoglycaemia, very high BP, "
    "suspected DKA, fetal movement reduction in pregnancy).\n"
    "- medium: side effects, dosing confusion needing clinician review.\n"
    "- low: routine adherence/refill/general updates with no symptoms.\n\n"
    "Reason is a short tag (≤40 chars), e.g. 'critical_red_flag', 'breathlessness', "
    "'dosing_confusion'. Output JSON matching the schema."
)

_COMPOSE_SYSTEM_PROMPT = (
    "You write a brief, empathetic reply to a patient WhatsApp message about their "
    "medication. Hard rules:\n"
    "- ≤500 characters.\n"
    "- Do NOT give medical advice or recommend medications/dosages.\n"
    "- Do NOT diagnose.\n"
    "- Plain text only — no markdown, no emoji.\n"
    "- If escalation is required, end with: 'Reply CALL now for urgent support.'\n"
    "- If template_mode is true, keep the tone formal and concise as this will be "
    "wrapped in an approved WhatsApp template.\n"
    "Output JSON with a single 'body' field."
)


class IntentClassification(BaseModel):
    intent: Intent
    confidence: Literal["low", "medium", "high"]


class SafetyAssessment(BaseModel):
    severity: RiskLevel
    reason: str = Field(min_length=1, max_length=80)


class ComposedReply(BaseModel):
    body: str = Field(min_length=1, max_length=600)


class DetectedLanguage(BaseModel):
    """LLM language-detection output. ``code`` is an ISO-639-1 string
    constrained to the i18n allowlist (the prompt enumerates the valid
    options); ``confidence`` lets the caller decide whether to act on
    a marginal guess. ``unknown`` is returned for short / ambiguous
    inputs we'd rather leave at the default."""

    code: Literal[
        "en",
        "hi",
        "ta",
        "te",
        "bn",
        "mr",
        "gu",
        "kn",
        "ml",
        "pa",
        "unknown",
    ]
    confidence: Literal["low", "medium", "high"]


class ParsedRegimen(BaseModel):
    """One medication line item extracted from a prescription image."""

    medication_name: str = Field(min_length=1, max_length=255)
    dose: str = Field(min_length=1, max_length=128)
    # Times of day the patient should take this dose, in 24h ``HH:MM``
    # format (the regimen.schedule.times shape). Empty when the schedule
    # couldn't be inferred.
    times_of_day: list[str] = Field(default_factory=list)
    frequency_text: str | None = Field(default=None, max_length=128)
    duration_days: int | None = Field(default=None, ge=1, le=365)
    notes: str | None = Field(default=None, max_length=500)


class ParsedPrescription(BaseModel):
    """Vision-LLM output for a prescription image."""

    confidence: Literal["high", "medium", "low"]
    regimens: list[ParsedRegimen] = Field(default_factory=list)
    summary: str | None = Field(default=None, max_length=300)
    illegible: bool = Field(default=False)


_PRESCRIPTION_SYSTEM_PROMPT = (
    "You are a medical assistant extracting medication orders from a "
    "prescription image. Return a structured ParsedPrescription.\n\n"
    "For each medication on the prescription, output one ParsedRegimen with:\n"
    "  - medication_name: the drug name as written (no abbreviations).\n"
    "  - dose: the strength + unit (e.g. '500 mg', '5 ml', '1 tablet').\n"
    "  - times_of_day: 24-hour HH:MM strings inferred from the frequency.\n"
    "      'once daily' → ['08:00']\n"
    "      'twice daily' / 'BID' → ['08:00', '20:00']\n"
    "      'three times daily' / 'TID' → ['08:00', '14:00', '20:00']\n"
    "      'four times daily' / 'QID' → ['08:00', '12:00', '16:00', '20:00']\n"
    "      'at bedtime' / 'HS' → ['22:00']\n"
    "      If frequency is unclear, leave times_of_day empty.\n"
    "  - frequency_text: original frequency text from the Rx.\n"
    "  - duration_days: integer days if specified (e.g. '×7 days' → 7), else null.\n"
    "  - notes: anything else relevant (e.g. 'with food', 'avoid alcohol').\n\n"
    "Confidence rules:\n"
    "  - high: image is clear, all fields legible, schedule unambiguous.\n"
    "  - medium: most fields legible but at least one inference (e.g. inferred\n"
    "    times_of_day from generic frequency).\n"
    "  - low: significant illegibility, missing fields, or ambiguity.\n\n"
    "Set ``illegible=true`` ONLY if the image is so unreadable that you can't\n"
    "extract any medication. In that case return regimens=[] and explain in summary.\n\n"
    "summary: one short sentence describing what you see (e.g. 'Two-medication "
    "Rx for diabetes; second dose timing unclear')."
)


class ParsePrescriptionResult(BaseModel):
    """Wrapper that flags transport failures distinctly from LLM-reported low confidence."""

    parsed: ParsedPrescription
    used_model: str


class AgentLLM:
    """Async LLM client for the orchestrator agent workflow."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("LLM_TIMEOUT_SECONDS", "10"))
        )
        if enabled is None:
            enabled = os.getenv("LLM_ENABLED", "1") != "0"
        self.enabled = bool(enabled and self.api_key)
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                log.warning("openai package not installed; LLM disabled")
                self.enabled = False
                return None
            self._client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout_seconds)
        return self._client

    async def classify_intent(self, text: str) -> Intent | None:
        if not self.enabled or not text:
            return None
        try:
            client = self._get_client()
            if client is None:
                return None
            from services.orchestrator.llm_tracking import track_llm_call

            async with track_llm_call(
                call_kind="classify_intent", model=self.model
            ) as tracker:
                completion = await client.chat.completions.parse(
                    model=self.model,
                    response_format=IntentClassification,
                    messages=[
                        {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                )
                tracker.set_completion(completion)
            parsed: IntentClassification | None = completion.choices[0].message.parsed
            if parsed is None:
                return None
            return parsed.intent
        except Exception as exc:  # noqa: BLE001 — fallback semantics
            log.warning("LLM intent classification failed: %s", exc)
            return None

    async def detect_language(self, text: str) -> tuple[str, str] | None:
        """Detect the language of an inbound text.

        Returns ``(code, confidence)`` from the i18n allowlist (or
        ``unknown`` for short / ambiguous inputs), or None on any
        failure. Caller is expected to gate on confidence — a low /
        medium guess on a short message is rarely worth flipping a
        patient's preference; high on a well-formed sentence is.

        This is a one-shot pass we run on a patient's first inbound
        when their preference is still the default. Subsequent
        messages aren't re-detected — the value is sticky once set so
        the patient's experience doesn't flicker if they later type
        a one-off message in another language.
        """
        if not self.enabled or not text:
            return None
        try:
            client = self._get_client()
            if client is None:
                return None
            from services.orchestrator.llm_tracking import track_llm_call

            async with track_llm_call(
                call_kind="detect_language", model=self.model
            ) as tracker:
                completion = await client.chat.completions.parse(
                    model=self.model,
                    response_format=DetectedLanguage,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You detect the language of a single short "
                                "message a patient sent over WhatsApp. Return "
                                "an ISO-639-1 code from this fixed list:\n"
                                "- en: English\n"
                                "- hi: Hindi (Devanagari OR Roman/Hinglish)\n"
                                "- ta: Tamil\n"
                                "- te: Telugu\n"
                                "- bn: Bengali\n"
                                "- mr: Marathi\n"
                                "- gu: Gujarati\n"
                                "- kn: Kannada\n"
                                "- ml: Malayalam\n"
                                "- pa: Punjabi\n"
                                "- unknown: too short, mixed beyond confidence, "
                                "or not in the list above\n\n"
                                "Confidence rules:\n"
                                "- high: full sentence with characteristic "
                                "vocabulary or non-Latin script.\n"
                                "- medium: short but unambiguous phrase.\n"
                                "- low: 1-3 words that COULD belong to several "
                                "languages — use ``unknown`` instead unless "
                                "the script is decisive.\n\n"
                                "Critical: action-tap markers like "
                                "``[dose-action] taken adherence_event_id=4`` "
                                "are protocol tokens, NOT a language signal. "
                                "Return ``unknown`` for those."
                            ),
                        },
                        {"role": "user", "content": text[:500]},
                    ],
                )
                tracker.set_completion(completion)
            parsed: DetectedLanguage | None = completion.choices[0].message.parsed
            if parsed is None:
                return None
            return parsed.code, parsed.confidence
        except Exception as exc:  # noqa: BLE001 — fallback semantics
            log.warning("LLM language detection failed: %s", exc)
            return None

    async def augment_safety(
        self, text: str, intent: str, current_severity: RiskLevel
    ) -> tuple[RiskLevel, str] | None:
        if not self.enabled or not text:
            return None
        try:
            client = self._get_client()
            if client is None:
                return None
            user_payload = (
                f"Rule-based floor severity: {current_severity}\n"
                f"Detected intent: {intent}\n"
                f"Patient message: {text}"
            )
            from services.orchestrator.llm_tracking import track_llm_call

            async with track_llm_call(
                call_kind="augment_safety", model=self.model
            ) as tracker:
                completion = await client.chat.completions.parse(
                    model=self.model,
                    response_format=SafetyAssessment,
                    messages=[
                        {"role": "system", "content": _SAFETY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_payload},
                    ],
                )
                tracker.set_completion(completion)
            parsed: SafetyAssessment | None = completion.choices[0].message.parsed
            if parsed is None:
                return None
            llm_rank = _SEVERITY_RANK[parsed.severity]
            floor_rank = _SEVERITY_RANK[current_severity]
            if llm_rank <= floor_rank:
                return current_severity, ""
            return parsed.severity, parsed.reason.strip().lower().replace(" ", "_")
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM safety augmentation failed: %s", exc)
            return None

    async def chat_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.2,
    ) -> dict[str, Any] | None:
        """Raw OpenAI chat-with-tools call. Returns the assistant message as a
        plain dict (so it slots straight back into ``state.messages``).

        Returns ``None`` on any failure — caller decides what to do (usually
        emit a friendly "having trouble, please try again" reply and exit the
        flow).
        """
        if not self.enabled:
            return None
        try:
            client = self._get_client()
            if client is None:
                return None
            from services.orchestrator.llm_tracking import track_llm_call

            async with track_llm_call(
                call_kind="booking_chat_with_tools", model=self.model
            ) as tracker:
                completion = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=temperature,
                )
                tracker.set_completion(completion)
            msg = completion.choices[0].message
            out: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM chat_with_tools failed: %s", exc)
            return None

    async def parse_prescription_image(
        self, *, image_url: str
    ) -> ParsePrescriptionResult | None:
        """Vision-LLM parse of a prescription image. Returns None on any
        failure (network, model doesn't support vision, structured-output
        parse error). Caller falls back to clinician manual entry.

        ``image_url`` must be publicly fetchable by OpenAI — pass the
        cloudflared-tunneled public URL of the uploaded image."""
        if not self.enabled:
            return None
        # Allow an env override so a user can point at a vision-capable
        # model (e.g. ``gpt-4o``) without affecting the chat model used
        # elsewhere. Falls back to ``self.model`` when unset.
        vision_model = os.getenv("OPENAI_VISION_MODEL") or self.model
        try:
            client = self._get_client()
            if client is None:
                return None
            from services.orchestrator.llm_tracking import track_llm_call

            async with track_llm_call(
                call_kind="prescription_vision", model=vision_model
            ) as tracker:
                completion = await client.chat.completions.parse(
                    model=vision_model,
                    response_format=ParsedPrescription,
                    messages=[
                        {"role": "system", "content": _PRESCRIPTION_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Extract the medications from this prescription.",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_url},
                                },
                            ],
                        },
                    ],
                )
                tracker.set_completion(completion)
            parsed: ParsedPrescription | None = completion.choices[0].message.parsed
            if parsed is None:
                return None
            return ParsePrescriptionResult(parsed=parsed, used_model=vision_model)
        except Exception as exc:  # noqa: BLE001 — fallback semantics
            log.warning("LLM prescription parse failed: %s", exc)
            return None

    async def compose_reply(
        self,
        *,
        intent: str,
        text: str,
        escalation_required: bool,
        use_template: bool,
        preferred_language: str | None = None,
    ) -> str | None:
        if not self.enabled or not text:
            return None
        try:
            from app import i18n

            client = self._get_client()
            if client is None:
                return None
            language_hint = i18n.llm_hint(preferred_language)
            # When the patient's profile prefers a non-English language
            # we put the language directive FIRST (before the body-rules
            # prompt) — language is a higher-priority constraint than
            # tone/format and the model otherwise tends to mirror the
            # input language. The directive explicitly overrides the
            # input language so an English-typing patient who has set
            # Hindi as their preference still gets a Hindi reply.
            messages: list[dict[str, str]] = []
            if preferred_language and preferred_language != i18n.DEFAULT_LANGUAGE_CODE:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"You MUST write the entire reply in "
                            f"{language_hint}, regardless of the language "
                            "the patient typed in. The patient has set "
                            f"{language_hint} as their preferred language "
                            "on their profile and may not read English "
                            "fluently. Use the native script — do NOT "
                            "transliterate to Roman. The escalation "
                            "phrase 'Reply CALL now for urgent support.' "
                            "stays in English verbatim because CALL is a "
                            "fixed protocol token."
                        ),
                    }
                )
            messages.append(
                {"role": "system", "content": _COMPOSE_SYSTEM_PROMPT}
            )
            user_payload = (
                f"intent: {intent}\n"
                f"escalation_required: {escalation_required}\n"
                f"template_mode: {use_template}\n"
                f"preferred_language: {preferred_language or 'en'}\n"
                f"patient_message: {text}"
            )
            messages.append({"role": "user", "content": user_payload})
            from services.orchestrator.llm_tracking import track_llm_call

            async with track_llm_call(
                call_kind="compose_reply", model=self.model
            ) as tracker:
                completion = await client.chat.completions.parse(
                    model=self.model,
                    response_format=ComposedReply,
                    messages=messages,
                )
                tracker.set_completion(completion)
            parsed: ComposedReply | None = completion.choices[0].message.parsed
            if parsed is None:
                return None
            body = parsed.body.strip()
            # The escalation tail is hard-coded English by design — it's
            # the SOS phrase. A non-English speaker should still recognise
            # ``CALL`` as the universal cue. If localising this turns out
            # to be important, register a translated phrase per language.
            if escalation_required and "CALL" not in body.upper():
                body = f"{body} Reply CALL now for urgent support."
            return body[:500]
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM compose failed: %s", exc)
            return None


_llm_singleton: Optional[AgentLLM] = None
_llm_lock = Lock()


def get_llm() -> AgentLLM:
    """Module-level lazy singleton so tests can monkeypatch one instance."""
    global _llm_singleton
    if _llm_singleton is None:
        with _llm_lock:
            if _llm_singleton is None:
                _llm_singleton = AgentLLM()
    return _llm_singleton


def reset_llm_for_tests() -> None:
    """Test helper — drop the cached singleton."""
    global _llm_singleton
    _llm_singleton = None
