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

export async function acknowledgeClinicalAlertAction(
  formData: FormData,
) {
  const alertId = int(formData.get("alert_id"));
  const actor = str(formData.get("actor")) || "ops";
  await orchestrator.acknowledgeClinicalAlert(alertId, { actor });
  revalidatePath("/clinical-alerts");
  revalidatePath(`/clinical-alerts/${alertId}`);
}

export async function resolveClinicalAlertAction(
  formData: FormData,
) {
  const alertId = int(formData.get("alert_id"));
  const actor = str(formData.get("actor")) || "ops";
  const notes = str(formData.get("notes")) || undefined;
  await orchestrator.resolveClinicalAlert(alertId, { actor, notes });
  revalidatePath("/clinical-alerts");
  revalidatePath(`/clinical-alerts/${alertId}`);
}

export async function pageClinicalAlertAction(formData: FormData) {
  const alertId = int(formData.get("alert_id"));
  await orchestrator.pageClinicalAlert(alertId);
  revalidatePath("/clinical-alerts");
}
