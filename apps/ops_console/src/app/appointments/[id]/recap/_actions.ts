"use server";

import { revalidatePath } from "next/cache";

import {
  orchestrator,
  type RecapLabItem,
  type RecapMedItem,
  type RecapStructuredPayload,
} from "@/lib/backend";

function parseLines(value: string): string[] {
  return value
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Each line: "<name> -- <instructions>" or "<name>".
 *  -- separator means "extra detail" (instructions for added, change for changed). */
function parseMedLines(value: string): RecapMedItem[] {
  return parseLines(value).map((line) => {
    const idx = line.indexOf("--");
    if (idx === -1) return { name: line };
    return {
      name: line.slice(0, idx).trim(),
      instructions: line.slice(idx + 2).trim() || null,
    };
  });
}

function parseLabLines(value: string): RecapLabItem[] {
  return parseLines(value).map((line) => ({ test_name: line }));
}

function readStructured(formData: FormData): RecapStructuredPayload {
  const nextDays = String(formData.get("next_followup_in_days") ?? "").trim();
  return {
    meds_added: parseMedLines(String(formData.get("meds_added") ?? "")),
    meds_changed: parseMedLines(String(formData.get("meds_changed") ?? "")),
    meds_stopped: parseMedLines(String(formData.get("meds_stopped") ?? "")),
    labs_ordered: parseLabLines(String(formData.get("labs_ordered") ?? "")),
    next_followup_in_days: nextDays ? Number(nextDays) : null,
    red_flags: parseLines(String(formData.get("red_flags") ?? "")),
  };
}

function getAppointmentId(formData: FormData): number {
  const raw = String(formData.get("appointment_id") ?? "");
  const id = Number(raw);
  if (!Number.isFinite(id) || id <= 0) {
    throw new Error("appointment_id is required");
  }
  return id;
}

export async function saveRecapAction(formData: FormData) {
  const id = getAppointmentId(formData);
  await orchestrator.upsertAppointmentRecap(id, {
    doctor_notes: String(formData.get("doctor_notes") ?? "").trim() || null,
    structured: readStructured(formData),
    authored_by:
      String(formData.get("authored_by") ?? "").trim() || null,
  });
  revalidatePath(`/appointments/${id}/recap`);
}

export async function previewRecapAction(formData: FormData) {
  const id = getAppointmentId(formData);
  // Always upsert first so the preview reflects the current form state.
  await orchestrator.upsertAppointmentRecap(id, {
    doctor_notes: String(formData.get("doctor_notes") ?? "").trim() || null,
    structured: readStructured(formData),
    authored_by:
      String(formData.get("authored_by") ?? "").trim() || null,
  });
  await orchestrator.previewAppointmentRecap(id);
  revalidatePath(`/appointments/${id}/recap`);
}

export async function sendRecapAction(formData: FormData) {
  const id = getAppointmentId(formData);
  // If the doctor edited fields then hit Send without a preview, save first.
  await orchestrator.upsertAppointmentRecap(id, {
    doctor_notes: String(formData.get("doctor_notes") ?? "").trim() || null,
    structured: readStructured(formData),
    authored_by:
      String(formData.get("authored_by") ?? "").trim() || null,
  });
  await orchestrator.sendAppointmentRecap(id);
  revalidatePath(`/appointments/${id}/recap`);
  revalidatePath(`/patients`);
}
