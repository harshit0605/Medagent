/**
 * Singleton chat-sdk Chat instance wired with the WhatsApp adapter.
 *
 * The Next.js webhook route (src/app/api/whatsapp/webhook/route.ts) delegates
 * GET (Meta verify-token handshake) and POST (signed inbound) to
 * `bot.webhooks.whatsapp(request)`. The adapter parses the payload and fires
 * `bot.onDirectMessage` for each new inbound. Our handler forwards the
 * message to the Python orchestrator's `/route` and ships the agent's reply
 * back through the Python gateway's `/send`.
 *
 * Outbound delivery (template + freeform) lives in Python so the WhatsApp
 * 24h customer-service-window policy stays in one place. Chat-sdk's own
 * `thread.post(...)` is intentionally NOT used.
 */

import {
  Chat,
  ConsoleLogger,
  type ActionEvent,
  type Message,
  type Thread,
} from "chat";
import { createWhatsAppAdapter } from "@chat-adapter/whatsapp";
import { createMemoryState } from "@chat-adapter/state-memory";

import { gateway, orchestrator } from "./backend";
import { downloadImageToPublic, transcribeAudio } from "./whatsapp-media";

declare global {
  // eslint-disable-next-line no-var
  var _medagentChatBot: Chat | undefined;
}

const REQUIRED_ENV = [
  "WHATSAPP_ACCESS_TOKEN",
  "WHATSAPP_APP_SECRET",
  "WHATSAPP_PHONE_NUMBER_ID",
  "WHATSAPP_VERIFY_TOKEN",
] as const;

export function missingWhatsAppEnv(): string[] {
  return REQUIRED_ENV.filter((name) => !(process.env[name] ?? "").trim());
}

function buildBot(): Chat {
  const userName = process.env.WHATSAPP_BOT_USERNAME ?? "medagent";
  const apiVersion = process.env.WHATSAPP_GRAPH_VERSION ?? "v22.0";

  const logger = new ConsoleLogger(
    (process.env.WHATSAPP_LOG_LEVEL as "debug" | "info" | "warn" | "error" | "silent" | undefined) ?? "info",
    "[chat-sdk:whatsapp]",
  );
  const adapter = createWhatsAppAdapter({
    accessToken: process.env.WHATSAPP_ACCESS_TOKEN!,
    appSecret: process.env.WHATSAPP_APP_SECRET!,
    phoneNumberId: process.env.WHATSAPP_PHONE_NUMBER_ID!,
    verifyToken: process.env.WHATSAPP_VERIFY_TOKEN!,
    userName,
    apiVersion,
    logger,
  });

  const chat = new Chat({
    userName,
    adapters: { whatsapp: adapter },
    state: createMemoryState(),
  });

  // For 1:1 WhatsApp threads chat-sdk fires onDirectMessage; older versions
  // route everything via onNewMention. Wire both for compatibility.
  chat.onDirectMessage(async (thread, message) => {
    await forwardInbound(message, thread);
  });
  chat.onNewMention(async (thread, message) => {
    await forwardInbound(message, thread);
  });

  // Interactive button taps (`reschedule_appt:3`, `cancel_appt:3`, `book_new`)
  // fire processAction, NOT onDirectMessage. Without this handler the user's
  // tap is silently dropped — chat-sdk has no caller and never invokes the
  // orchestrator. We translate the actionId into a precise text instruction
  // and run the same orchestrator → gateway pipeline as text.
  chat.onAction(async (event) => {
    await forwardButtonAction(event);
  });

  return chat;
}

export function getBot(): Chat | null {
  if (missingWhatsAppEnv().length > 0) return null;
  if (!globalThis._medagentChatBot) {
    globalThis._medagentChatBot = buildBot();
  }
  return globalThis._medagentChatBot;
}

/** Extract the user's WhatsApp ID from a chat-sdk thread.id of shape
 *  `whatsapp:{phoneNumberId}:{userWaId}`. Falls back to the raw thread id. */
function userWaIdFrom(threadId: string): string {
  const parts = threadId.split(":");
  return parts.length >= 3 ? parts[parts.length - 1] : threadId;
}

/** Pull an audio media id off chat-sdk's `message.raw`. The WhatsApp adapter
 *  wraps the inbound payload as ``{ contact?, message: WhatsAppInboundMessage,
 *  phoneNumberId }``, so the audio attachment lives at ``raw.message.audio.id``.
 *  chat-sdk's normalised ``text`` is empty for audio messages, so we transcribe
 *  via Whisper and use that as the patient's message before routing. */
function audioMediaIdFrom(message: Message): string | null {
  const raw = message.raw as
    | { message?: { audio?: { id?: string }; type?: string } }
    | undefined;
  return raw?.message?.audio?.id ?? null;
}

/** Pull an image media id off chat-sdk's `message.raw` (e.g. a prescription
 *  photo). Same envelope shape as audio. */
function imageMediaIdFrom(message: Message): string | null {
  const raw = message.raw as
    | { message?: { image?: { id?: string; caption?: string } } }
    | undefined;
  return raw?.message?.image?.id ?? null;
}

function imageCaptionFrom(message: Message): string | null {
  const raw = message.raw as
    | { message?: { image?: { caption?: string } } }
    | undefined;
  return raw?.message?.image?.caption ?? null;
}

/** Pull an interactive button reply id off chat-sdk's `message.raw`.
 *  Meta puts the tapped button under
 *  ``raw.message.interactive.button_reply.id`` for reply-button taps and
 *  ``raw.message.interactive.list_reply.id`` for list-message picks. The
 *  visible label travels via ``message.text`` (button title) — but we ship
 *  context-rich button ids like ``reschedule_appt:3`` that encode the action
 *  and the target appointment, and chat-sdk only surfaces the title. We read
 *  the id ourselves and rewrite the inbound text downstream. */
function interactiveReplyIdFrom(message: Message): string | null {
  const raw = message.raw as
    | {
        message?: {
          interactive?: {
            type?: string;
            button_reply?: { id?: string };
            list_reply?: { id?: string };
          };
        };
      }
    | undefined;
  const interactive = raw?.message?.interactive;
  if (!interactive) return null;
  return (
    interactive.button_reply?.id ?? interactive.list_reply?.id ?? null
  );
}

/** Map our button-id / list-row-id convention onto an unambiguous text
 *  instruction the orchestrator can route on. Returns null when the id
 *  doesn't match a known prefix — in that case the original
 *  ``message.text`` (button or row title) is left as-is.
 *
 *  Conventions emitted by the orchestrator + scheduler:
 *    Booking flow:
 *      cancel_appt:{N}            → reply button
 *      reschedule_appt:{N}        → reply button
 *      book_new                   → reply button
 *      slot_book:{doc}|{s}|{e}    → list row (book a new slot)
 *      slot_resched:{appt}|{doc}|{s}|{e} → list row (reschedule existing)
 *    Dose / adherence:
 *      dose_taken:{adherence_event_id}    → reply button
 *      dose_snoozed:{adherence_event_id}  → reply button
 *      dose_skipped:{adherence_event_id}  → reply button
 *
 *  Dose-button text intentionally starts with "[dose-action]" so the
 *  orchestrator's deterministic dose handler can short-circuit BEFORE the
 *  LLM intent classifier — these are pure CRUD actions, no LLM needed. */
function textFromButtonId(buttonId: string): string | null {
  if (buttonId.startsWith("cancel_appt:")) {
    const apptId = buttonId.slice("cancel_appt:".length).trim();
    if (!apptId) return null;
    return `Please cancel my appointment id ${apptId}.`;
  }
  if (buttonId.startsWith("reschedule_appt:")) {
    const apptId = buttonId.slice("reschedule_appt:".length).trim();
    if (!apptId) return null;
    return `Please reschedule my appointment id ${apptId}.`;
  }
  if (buttonId === "book_new") {
    return "I want to book a new appointment.";
  }
  if (buttonId.startsWith("slot_book:")) {
    const parts = buttonId.slice("slot_book:".length).split("|");
    if (parts.length !== 3) return null;
    const [docId, startIso, endIso] = parts;
    if (!docId || !startIso || !endIso) return null;
    return `Please book the slot ${startIso} to ${endIso} with doctor ${docId}.`;
  }
  if (buttonId.startsWith("slot_resched:")) {
    const parts = buttonId.slice("slot_resched:".length).split("|");
    if (parts.length !== 4) return null;
    const [apptId, docId, startIso, endIso] = parts;
    if (!apptId || !docId || !startIso || !endIso) return null;
    return `Please reschedule appointment id ${apptId} to ${startIso} to ${endIso} with doctor ${docId}.`;
  }
  if (buttonId.startsWith("dose_taken:")) {
    const id = buttonId.slice("dose_taken:".length).trim();
    if (!id) return null;
    return `[dose-action] taken adherence_event_id=${id}`;
  }
  if (buttonId.startsWith("dose_snoozed:")) {
    const id = buttonId.slice("dose_snoozed:".length).trim();
    if (!id) return null;
    return `[dose-action] snoozed adherence_event_id=${id}`;
  }
  if (buttonId.startsWith("dose_skipped:")) {
    const id = buttonId.slice("dose_skipped:".length).trim();
    if (!id) return null;
    return `[dose-action] skipped adherence_event_id=${id}`;
  }
  if (buttonId.startsWith("dose_late_taken:")) {
    // Recovery button shown on the "already marked X" reply when the patient
    // tapped after the grace window. Overrides missed/skipped/delayed → taken.
    const id = buttonId.slice("dose_late_taken:".length).trim();
    if (!id) return null;
    return `[dose-action] late_taken adherence_event_id=${id}`;
  }
  if (buttonId.startsWith("refill_done:")) {
    const id = buttonId.slice("refill_done:".length).trim();
    if (!id) return null;
    return `[refill-action] done regimen_id=${id}`;
  }
  if (buttonId.startsWith("refill_snoozed:")) {
    const id = buttonId.slice("refill_snoozed:".length).trim();
    if (!id) return null;
    return `[refill-action] snoozed regimen_id=${id}`;
  }
  if (buttonId.startsWith("refill_help:")) {
    const id = buttonId.slice("refill_help:".length).trim();
    if (!id) return null;
    return `[refill-action] help regimen_id=${id}`;
  }
  if (buttonId.startsWith("lab_booked:")) {
    const id = buttonId.slice("lab_booked:".length).trim();
    if (!id) return null;
    return `[lab-action] booked lab_followup_id=${id}`;
  }
  if (buttonId.startsWith("lab_completed:")) {
    const id = buttonId.slice("lab_completed:".length).trim();
    if (!id) return null;
    return `[lab-action] completed lab_followup_id=${id}`;
  }
  if (buttonId.startsWith("lab_help:")) {
    const id = buttonId.slice("lab_help:".length).trim();
    if (!id) return null;
    return `[lab-action] help lab_followup_id=${id}`;
  }
  // Recap quick-reply buttons (sent on out-of-CSW recap template_v2).
  // The orchestrator's recap_handler matches both the marker form and
  // plain "OK" / "QUESTION" replies; the marker carries the recap_id
  // so the right row is updated even if multiple are pending.
  if (buttonId.startsWith("recap-ack-")) {
    const id = buttonId.slice("recap-ack-".length).trim();
    if (!id) return null;
    return `[recap-action] ack recap_id=${id}`;
  }
  if (buttonId.startsWith("recap-question-")) {
    const id = buttonId.slice("recap-question-".length).trim();
    if (!id) return null;
    return `[recap-action] question recap_id=${id}`;
  }
  // Caregiver consent prompt buttons (sent on caregiver_consent_v1
  // template). The orchestrator's caregiver_handler picks up the
  // marker form keyed on caregiver_id so the right pending row flips
  // even if the caregiver phone has multiple records.
  if (buttonId.startsWith("caregiver-confirm:")) {
    const id = buttonId.slice("caregiver-confirm:".length).trim();
    if (!id) return null;
    return `[caregiver-action] confirm caregiver_id=${id}`;
  }
  if (buttonId.startsWith("caregiver-decline:")) {
    const id = buttonId.slice("caregiver-decline:".length).trim();
    if (!id) return null;
    return `[caregiver-action] decline caregiver_id=${id}`;
  }
  return null;
}

async function forwardInbound(
  message: Message,
  thread: Thread,
): Promise<void> {
  const fromPhone = userWaIdFrom(thread.id);
  let text = message.text ?? "";
  // ``inputKind`` describes HOW the patient sent this — drives the
  // doctor-inbox row's badge so a clinician can spot transcribed-audio
  // entries (and audit Whisper quality) without reopening the audio.
  let inputKind: "text" | "voice" | "image" | "button" = "text";

  // chat-sdk normalises audio inbound to a "[Audio message]" placeholder.
  // If we see an audio media id we always prefer the transcribed body —
  // the placeholder is useless to the LLM.
  const mediaId = audioMediaIdFrom(message);
  if (mediaId) {
    const transcript = await transcribeAudio(mediaId);
    if (transcript) {
      console.log(
        "[whatsapp] transcribed audio for message %s (%d chars)",
        message.id,
        transcript.length,
      );
      text = transcript;
      inputKind = "voice";
    } else {
      console.warn(
        "[whatsapp] audio transcription returned empty for message %s — sending fallback reply",
        message.id,
      );
      // Empty transcripts used to fall through to the orchestrator with
      // text="" — the LLM would then produce a generic "tell me more"
      // reply. That hides the failure from the patient. Better: tell
      // them directly, in-CSW only (the audio inbound itself opens the
      // 24h window, so freeform is allowed). Skip /route entirely so
      // the inbox doesn't get a noise row for an unprocessable input.
      try {
        await gateway.send({
          phone: fromPhone,
          body:
            "Sorry — I couldn't make out that voice note. Could you try again or type your message?",
          use_template: false,
        });
      } catch (err) {
        console.error(
          "[whatsapp] empty-transcript fallback send failed for %s: %s",
          message.id,
          err,
        );
      }
      return;
    }
  }

  // Defensive belt-and-suspenders: in case any inbound interactive reply
  // ever lands here (some chat-sdk versions / payload shapes route it via
  // onDirectMessage), still rewrite from the button id. The primary path is
  // the chat.onAction handler in buildBot — see forwardButtonAction below.
  const buttonId = interactiveReplyIdFrom(message);
  if (buttonId) {
    const rewritten = textFromButtonId(buttonId);
    if (rewritten) {
      console.log(
        "[whatsapp] button tap (via onDirectMessage) %s on message %s — rewriting text to: %s",
        buttonId,
        message.id,
        rewritten,
      );
      text = rewritten;
      inputKind = "button";
    }
  }

  // Image inbound (e.g. a prescription photo). Download to public/uploads/
  // and rewrite text to a marker the orchestrator's prescription handler
  // can short-circuit on. The marker carries:
  //   - public_path (Next.js-served URL the vision LLM can fetch)
  //   - mime_type
  //   - caption (original WhatsApp caption, if any)
  // Orchestrator parses by regex (see services/orchestrator/prescription_handler.py).
  const imageMediaId = imageMediaIdFrom(message);
  if (imageMediaId) {
    const saved = await downloadImageToPublic(imageMediaId);
    if (saved) {
      const caption = imageCaptionFrom(message) ?? "";
      const captionPart = caption ? ` caption=${JSON.stringify(caption)}` : "";
      text = `[prescription-upload] public_path=${saved.publicPath} mime=${saved.mimeType}${captionPart}`;
      inputKind = "image";
      console.log(
        "[whatsapp] saved inbound image %s → %s",
        imageMediaId,
        saved.publicPath,
      );
    } else {
      console.warn(
        "[whatsapp] image download failed for media id %s — falling through to text path",
        imageMediaId,
      );
    }
  }

  await runOrchestratorPipeline({
    fromPhone,
    text,
    messageId: message.id,
    inputKind,
  });
}

async function forwardButtonAction(event: ActionEvent): Promise<void> {
  const fromPhone = event.user.userId || userWaIdFrom(event.threadId);
  // chat-sdk passes the raw button id as `actionId` when the id wasn't a
  // chat-sdk-encoded JSON envelope (which is our case — we ship plain ids
  // like ``reschedule_appt:3``). Fall back to ``value`` for safety.
  const buttonId = event.actionId || event.value || "";
  const rewritten = textFromButtonId(buttonId);
  const text = rewritten ?? event.value ?? buttonId;
  console.log(
    "[whatsapp] button action %s from %s — text: %s",
    buttonId,
    fromPhone,
    text,
  );
  await runOrchestratorPipeline({
    fromPhone,
    text,
    messageId: event.messageId,
    inputKind: "button",
  });
}

async function runOrchestratorPipeline(args: {
  fromPhone: string;
  text: string;
  messageId: string;
  inputKind?: "text" | "voice" | "image" | "button";
}): Promise<void> {
  const { fromPhone, text, messageId, inputKind } = args;
  let routeResult;
  try {
    routeResult = await orchestrator.route({
      message: {
        message_id: messageId,
        patient_id: fromPhone,
        phone: fromPhone,
        text,
        input_kind: inputKind,
      },
    });
  } catch (err) {
    console.error(
      "[whatsapp] orchestrator /route failed for message %s: %s",
      messageId,
      err,
    );
    return;
  }

  try {
    await gateway.send(routeResult.message_out);
  } catch (err) {
    console.error(
      "[whatsapp] gateway /send failed for message %s: %s",
      messageId,
      err,
    );
  }
}
