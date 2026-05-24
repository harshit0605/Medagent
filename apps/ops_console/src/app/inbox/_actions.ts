"use server";

import { revalidatePath } from "next/cache";

import { orchestrator, type DraftReply } from "@/lib/backend";

// Actor recorded for ops-console-initiated inbox actions. There's no
// per-operator identity layer yet (shared-key auth), so this is a fixed
// label — see the orchestrator-side note on DSAR attribution.
const OPS_ACTOR = "ops_console";

function int(value: FormDataEntryValue | null): number {
  const n = Number(value);
  if (!Number.isFinite(n) || !Number.isInteger(n)) {
    throw new Error(`expected integer, got ${value!.toString()}`);
  }
  return n;
}

function str(value: FormDataEntryValue | null): string {
  return String(value ?? "").trim();
}

export async function sendDoctorReplyAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const bodyText = str(formData.get("body"));
  const sentBy = str(formData.get("sent_by")) || "ops";
  const inReplyTo = str(formData.get("in_reply_to_message_id")) || null;
  if (!bodyText) {
    throw new Error("reply body is required");
  }

  await orchestrator.sendDoctorReply(patientId, {
    body: bodyText,
    sent_by: sentBy,
    in_reply_to_message_id: inReplyTo,
  });
  revalidatePath("/inbox");
  revalidatePath(`/patients/${patientId}`);
}

// Programmatic actions (called directly from client components, not via a
// form submit) so the client never imports the server-only backend client.

export async function setInboxFeedbackAction(
  classificationId: number,
  rating: -1 | 1,
): Promise<void> {
  await orchestrator.setInboxFeedback(classificationId, {
    rating,
    actor: OPS_ACTOR,
  });
}

export async function clearInboxFeedbackAction(
  classificationId: number,
): Promise<void> {
  await orchestrator.clearInboxFeedback(classificationId);
}

export async function draftInboxReplyAction(
  classificationId: number,
): Promise<DraftReply> {
  return orchestrator.draftInboxReply(classificationId);
}
