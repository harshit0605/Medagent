"use server";

import { revalidatePath } from "next/cache";

import { orchestrator, type ParsedRegimen } from "@/lib/backend";

function parseTimesOfDay(raw: string): string[] {
  return raw
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export async function verifyPrescriptionAction(formData: FormData) {
  const prescriptionId = Number(formData.get("prescription_id"));
  if (!prescriptionId || Number.isNaN(prescriptionId)) {
    throw new Error("verifyPrescriptionAction: invalid prescription_id");
  }
  const verifiedBy = String(formData.get("verified_by") ?? "ops").trim() || "ops";
  const timezone = String(formData.get("timezone") ?? "Asia/Kolkata").trim();

  // Pull each ROW-N field (med, dose, times). Empty rows are skipped.
  const indexes = Array.from(
    new Set(
      Array.from(formData.keys())
        .map((k) => /^medication_name_(\d+)$/.exec(k)?.[1])
        .filter((s): s is string => Boolean(s)),
    ),
  ).map((s) => Number(s)).sort((a, b) => a - b);

  const regimens: ParsedRegimen[] = [];
  for (const i of indexes) {
    const med = String(formData.get(`medication_name_${i}`) ?? "").trim();
    const dose = String(formData.get(`dose_${i}`) ?? "").trim();
    const timesRaw = String(formData.get(`times_of_day_${i}`) ?? "").trim();
    const freq = String(formData.get(`frequency_text_${i}`) ?? "").trim();
    const durRaw = String(formData.get(`duration_days_${i}`) ?? "").trim();
    const notes = String(formData.get(`notes_${i}`) ?? "").trim();
    if (!med || !dose) continue;
    regimens.push({
      medication_name: med,
      dose,
      times_of_day: parseTimesOfDay(timesRaw),
      frequency_text: freq || null,
      duration_days: durRaw ? Number(durRaw) : null,
      notes: notes || null,
    });
  }

  if (regimens.length === 0) {
    throw new Error(
      "verifyPrescriptionAction: at least one regimen with medication + dose is required",
    );
  }

  await orchestrator.verifyPrescription(prescriptionId, {
    verified_by: verifiedBy,
    regimens,
    timezone,
  });
  revalidatePath("/prescriptions");
  revalidatePath(`/prescriptions/${prescriptionId}`);
}

export async function rejectPrescriptionAction(formData: FormData) {
  const prescriptionId = Number(formData.get("prescription_id"));
  if (!prescriptionId || Number.isNaN(prescriptionId)) {
    throw new Error("rejectPrescriptionAction: invalid prescription_id");
  }
  const rejectedBy = String(formData.get("rejected_by") ?? "ops").trim() || "ops";
  const reason = String(formData.get("reason") ?? "").trim() || undefined;
  await orchestrator.rejectPrescription(prescriptionId, {
    rejected_by: rejectedBy,
    reason,
  });
  revalidatePath("/prescriptions");
  revalidatePath(`/prescriptions/${prescriptionId}`);
}
