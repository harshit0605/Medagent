"use server";

import { revalidatePath } from "next/cache";

import { orchestrator } from "@/lib/backend";

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
