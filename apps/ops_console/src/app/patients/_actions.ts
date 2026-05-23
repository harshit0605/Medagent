"use server";

import { revalidatePath } from "next/cache";

import { orchestrator } from "@/lib/backend";

function parseTimes(raw: string): string[] {
  // Accept comma-separated "08:00, 20:00" or whitespace-separated "08:00 20:00".
  return raw
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export async function addRegimenAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  if (!patientId || Number.isNaN(patientId)) {
    throw new Error("addRegimenAction: invalid patient_id");
  }
  const medication_name = String(formData.get("medication_name") ?? "").trim();
  const dose = String(formData.get("dose") ?? "").trim();
  const timesRaw = String(formData.get("times") ?? "").trim();
  const timezone = String(formData.get("timezone") ?? "Asia/Kolkata").trim();
  if (!medication_name || !dose || !timesRaw) {
    throw new Error(
      "addRegimenAction: medication_name, dose, and times are required",
    );
  }
  const times = parseTimes(timesRaw);
  if (times.length === 0) {
    throw new Error("addRegimenAction: at least one time is required");
  }
  // Optional supply tracking. Empty string ⇒ omit so the patient's regimen
  // is created without refill reminders. When supply_days is provided but
  // supply_started_on isn't, the orchestrator defaults the start to today.
  const supplyDaysRaw = String(formData.get("supply_days_initial") ?? "").trim();
  const supplyStartedRaw = String(
    formData.get("supply_started_on") ?? "",
  ).trim();
  const supply_days_initial = supplyDaysRaw ? Number(supplyDaysRaw) : null;
  const supply_started_on = supplyStartedRaw || null;
  if (
    supply_days_initial !== null &&
    (Number.isNaN(supply_days_initial) || supply_days_initial < 1)
  ) {
    throw new Error("addRegimenAction: supply_days_initial must be a positive integer");
  }

  await orchestrator.createRegimen(patientId, {
    medication_name,
    dose,
    schedule: {
      type: "times_of_day",
      times,
      timezone,
      frequency: "daily",
    },
    supply_days_initial,
    supply_started_on,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function deactivateRegimenAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  const regimenId = Number(formData.get("regimen_id"));
  if (!patientId || !regimenId) {
    throw new Error("deactivateRegimenAction: invalid ids");
  }
  await orchestrator.deactivateRegimen(regimenId);
  revalidatePath(`/patients/${patientId}`);
}

export async function fireTestDoseAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  const regimenId = Number(formData.get("regimen_id"));
  if (!patientId || !regimenId) {
    throw new Error("fireTestDoseAction: invalid ids");
  }
  await orchestrator.fireTestDose(regimenId);
  revalidatePath(`/patients/${patientId}`);
}

export async function fireTestRefillAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  const regimenId = Number(formData.get("regimen_id"));
  if (!patientId || !regimenId) {
    throw new Error("fireTestRefillAction: invalid ids");
  }
  await orchestrator.fireTestRefill(regimenId);
  revalidatePath(`/patients/${patientId}`);
}

export async function addLabFollowupAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  if (!patientId || Number.isNaN(patientId)) {
    throw new Error("addLabFollowupAction: invalid patient_id");
  }
  const test_name = String(formData.get("test_name") ?? "").trim();
  const due_by_raw = String(formData.get("due_by") ?? "").trim();
  const notes_raw = String(formData.get("notes") ?? "").trim();
  if (!test_name) {
    throw new Error("addLabFollowupAction: test_name is required");
  }
  await orchestrator.createLabFollowup(patientId, {
    test_name,
    due_by: due_by_raw || null,
    notes: notes_raw || null,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function markLabCompletedAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  const labId = Number(formData.get("lab_id"));
  if (!patientId || !labId) {
    throw new Error("markLabCompletedAction: invalid ids");
  }
  await orchestrator.markLabCompleted(labId);
  revalidatePath(`/patients/${patientId}`);
}

export async function markLabReviewedAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  const labId = Number(formData.get("lab_id"));
  if (!patientId || !labId) {
    throw new Error("markLabReviewedAction: invalid ids");
  }
  await orchestrator.markLabReviewed(labId);
  revalidatePath(`/patients/${patientId}`);
}

export async function fireTestLabReminderAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  const labId = Number(formData.get("lab_id"));
  if (!patientId || !labId) {
    throw new Error("fireTestLabReminderAction: invalid ids");
  }
  await orchestrator.fireTestLabReminder(labId);
  revalidatePath(`/patients/${patientId}`);
}

export async function resetOnboardingAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  if (!patientId || Number.isNaN(patientId)) {
    throw new Error("resetOnboardingAction: invalid patient_id");
  }
  await orchestrator.resetPatientOnboarding(patientId);
  revalidatePath(`/patients/${patientId}`);
}

export async function pauseBotAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  if (!patientId || Number.isNaN(patientId)) {
    throw new Error("pauseBotAction: invalid patient_id");
  }
  const actor = String(formData.get("actor") ?? "").trim() || "ops";
  const reason = String(formData.get("reason") ?? "").trim();
  if (!reason) {
    throw new Error("pause reason is required");
  }
  await orchestrator.pauseBot(patientId, { actor, reason });
  revalidatePath(`/patients/${patientId}`);
}

export async function unpauseBotAction(formData: FormData) {
  const patientId = Number(formData.get("patient_id"));
  if (!patientId || Number.isNaN(patientId)) {
    throw new Error("unpauseBotAction: invalid patient_id");
  }
  await orchestrator.unpauseBot(patientId);
  revalidatePath(`/patients/${patientId}`);
}
