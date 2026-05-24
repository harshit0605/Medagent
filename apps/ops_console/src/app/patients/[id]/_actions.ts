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

export async function createExemptionAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const carePlanId = int(formData.get("care_plan_id"));
  const reason = str(formData.get("reason"));
  if (!reason) throw new Error("reason is required");

  const expiresRaw = str(formData.get("expires_at"));
  // The form's <input type="date"> gives YYYY-MM-DD; we send midnight UTC
  // for that day so the exemption expires at the start of the chosen
  // day. (Better than 23:59 because clinicians think in days, not hours.)
  const expires_at = expiresRaw ? `${expiresRaw}T00:00:00Z` : null;

  await orchestrator.createPatientExemption(patientId, {
    care_plan_id: carePlanId,
    reason,
    expires_at,
    created_by: str(formData.get("created_by")) || null,
  });
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/");
}

export async function revokeExemptionAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const exemptionId = int(formData.get("exemption_id"));
  await orchestrator.revokePatientExemption(exemptionId, {
    revoked_by: str(formData.get("revoked_by")) || null,
  });
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/");
}

export async function assignCohortTagAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const tagId = int(formData.get("cohort_tag_id"));
  await orchestrator.assignPatientCohortTag(patientId, {
    cohort_tag_id: tagId,
    assigned_by: str(formData.get("assigned_by")) || null,
  });
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/cohort-tags");
}

export async function removeCohortTagAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const tagId = int(formData.get("cohort_tag_id"));
  await orchestrator.removePatientCohortTag(patientId, tagId);
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/cohort-tags");
}

export async function addCaregiverAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const fullName = str(formData.get("full_name"));
  const phone = str(formData.get("phone"));
  if (!fullName || !phone) {
    throw new Error("name and phone are required");
  }
  await orchestrator.createCaregiver(patientId, {
    full_name: fullName,
    phone,
    relationship_to_patient:
      str(formData.get("relationship_to_patient")) || null,
    notify_on_recap: formData.get("notify_on_recap") === "on",
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function confirmCaregiverConsentAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const caregiverId = int(formData.get("caregiver_id"));
  const confirmedBy = str(formData.get("confirmed_by")) || "ops";
  await orchestrator.confirmCaregiverConsent(caregiverId, {
    confirmed_by: confirmedBy,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function revokeCaregiverConsentAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const caregiverId = int(formData.get("caregiver_id"));
  await orchestrator.revokeCaregiverConsent(caregiverId);
  revalidatePath(`/patients/${patientId}`);
}

export async function sendCaregiverConsentPromptAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const caregiverId = int(formData.get("caregiver_id"));
  await orchestrator.sendCaregiverConsentPrompt(caregiverId);
  revalidatePath(`/patients/${patientId}`);
}

export async function updatePatientLanguageAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const code = str(formData.get("preferred_language"));
  if (!code) {
    throw new Error("preferred_language is required");
  }
  await orchestrator.updatePatientLanguage(patientId, {
    preferred_language: code,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function deactivateCaregiverAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const caregiverId = int(formData.get("caregiver_id"));
  await orchestrator.updateCaregiver(caregiverId, { active: false });
  revalidatePath(`/patients/${patientId}`);
}

export async function setCaregiverNotifyAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const caregiverId = int(formData.get("caregiver_id"));
  const enable = formData.get("enable") === "true";
  await orchestrator.updateCaregiver(caregiverId, {
    notify_on_recap: enable,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function createPatientGoalAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const metricKey = str(formData.get("metric_key"));
  const metricLabel = str(formData.get("metric_label"));
  const targetRaw = str(formData.get("target_value"));
  const comparator = str(formData.get("comparator")) || "less_than";
  const targetUnit = str(formData.get("target_unit"));
  const notes = str(formData.get("notes")) || null;
  const createdBy = str(formData.get("created_by")) || "ops";
  const endsOnRaw = str(formData.get("ends_on")) || null;

  if (!metricKey) throw new Error("metric_key is required");
  if (!metricLabel) throw new Error("metric_label is required");
  if (!targetUnit) throw new Error("target_unit is required");
  const targetValue = Number(targetRaw);
  if (!Number.isFinite(targetValue)) {
    throw new Error("target_value must be a number");
  }
  if (comparator !== "less_than" && comparator !== "greater_than") {
    throw new Error(
      "comparator must be 'less_than' or 'greater_than'",
    );
  }

  await orchestrator.createPatientGoal(patientId, {
    metric_key: metricKey,
    metric_label: metricLabel,
    target_value: targetValue,
    comparator,
    target_unit: targetUnit,
    notes,
    created_by: createdBy,
    ends_on: endsOnRaw,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function recordGoalObservationAction(
  formData: FormData,
) {
  const patientId = int(formData.get("patient_id"));
  const goalId = int(formData.get("goal_id"));
  const valueRaw = str(formData.get("value"));
  const unit = str(formData.get("unit"));
  const observedAtRaw = str(formData.get("observed_at")) || null;
  const notes = str(formData.get("notes")) || null;
  const recordedBy = str(formData.get("recorded_by")) || "ops";

  const value = Number(valueRaw);
  if (!Number.isFinite(value)) {
    throw new Error("value must be a number");
  }
  if (!unit) throw new Error("unit is required");

  await orchestrator.recordGoalObservation(patientId, goalId, {
    value,
    unit,
    observed_at: observedAtRaw
      ? new Date(observedAtRaw).toISOString()
      : undefined,
    notes,
    recorded_by: recordedBy,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function updatePatientGoalStatusAction(
  formData: FormData,
) {
  const patientId = int(formData.get("patient_id"));
  const goalId = int(formData.get("goal_id"));
  const status = str(formData.get("status"));
  if (
    status !== "active" &&
    status !== "achieved" &&
    status !== "inactive"
  ) {
    throw new Error("invalid status");
  }
  await orchestrator.updatePatientGoalStatus(
    patientId,
    goalId,
    status,
  );
  revalidatePath(`/patients/${patientId}`);
}

export async function generateVisitBriefAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const windowDaysRaw = str(formData.get("window_days"));
  const generatedBy = str(formData.get("generated_by")) || "ops";
  const windowDays = windowDaysRaw ? Number(windowDaysRaw) : 30;
  if (!Number.isInteger(windowDays) || windowDays < 1 || windowDays > 180) {
    throw new Error("window_days must be an integer in [1, 180]");
  }
  await orchestrator.generateVisitBrief(patientId, {
    window_days: windowDays,
    generated_by: generatedBy,
  });
  revalidatePath(`/patients/${patientId}`);
}

export async function sendDoctorReplyAction(formData: FormData) {
  const patientId = int(formData.get("patient_id"));
  const bodyText = str(formData.get("body"));
  const sentBy = str(formData.get("sent_by")) || "ops";
  if (!bodyText) {
    throw new Error("reply body is required");
  }
  await orchestrator.sendDoctorReply(patientId, {
    body: bodyText,
    sent_by: sentBy,
  });
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/inbox");
}

// DSAR right-of-access export. Returns the assembled JSON document so the
// client component can build the downloadable Blob — the network call +
// API key stay on the server (the client never imports the backend client).
export async function exportPatientAction(
  patientId: number,
): Promise<Record<string, unknown>> {
  return orchestrator.exportPatient(patientId, {
    actor: "ops_console",
    window_days: 365,
  });
}

// Right-of-erasure. Irreversible; the client component already gates this
// behind a typed-name + reason + acknowledgement confirm panel.
export async function erasePatientAction(
  patientId: number,
  reason: string,
): Promise<void> {
  const trimmed = reason.trim();
  if (!trimmed) {
    throw new Error("reason is required");
  }
  await orchestrator.erasePatient(patientId, {
    actor: "ops_console",
    reason: trimmed,
    confirm: true,
  });
  revalidatePath(`/patients/${patientId}`);
  revalidatePath("/");
}
