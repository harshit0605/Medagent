from enum import Enum


class ChannelType(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"
    MOBILE_APP = "mobile_app"


class MessageContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"


class MessageMode(str, Enum):
    TEMPLATE = "template"
    FREEFORM = "freeform"


class MessageCategory(str, Enum):
    UTILITY = "utility"
    SERVICE = "service"
    MARKETING = "marketing"
    AUTHENTICATION = "authentication"


class Intent(str, Enum):
    DOSE_CONFIRMATION = "dose_confirmation"
    DOSE_MISSED = "dose_missed"
    REFILL_REQUEST = "refill_request"
    TRIAGE_SUPPORT = "triage_support"
    UNKNOWN = "unknown"


class EventType(str, Enum):
    DOSE_DUE = "dose_due"
    DOSE_CONFIRMED = "dose_confirmed"
    DOSE_MISSED = "dose_missed"
    REFILL_DUE = "refill_due"
    TRIAGE_ALERT = "triage_alert"
    FOLLOWUP_CLOSURE = "followup_closure"
