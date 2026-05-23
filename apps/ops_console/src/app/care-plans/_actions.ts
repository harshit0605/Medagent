"use server";

import { revalidatePath } from "next/cache";

import { orchestrator } from "@/lib/backend";

function int(value: FormDataEntryValue | null, fallback?: number): number {
  if (value === null || value === "") {
    if (fallback !== undefined) return fallback;
    throw new Error("missing required integer field");
  }
  const n = Number(value);
  if (!Number.isFinite(n) || !Number.isInteger(n)) {
    throw new Error(`expected integer, got ${value!.toString()}`);
  }
  return n;
}

function str(value: FormDataEntryValue | null): string {
  return String(value ?? "").trim();
}

export async function createCarePlanAction(formData: FormData) {
  // ``cohort_choice`` is encoded as either "attr:<column>" or "tag:<id>"
  // by the picker (see /care-plans/page.tsx). Decode and dispatch to
  // the right field on the orchestrator request.
  const cohortChoice = str(formData.get("cohort_choice"));
  const test_name = str(formData.get("test_name"));
  const cadence_days = int(formData.get("cadence_days"));
  const due_in_days = int(formData.get("due_in_days"), 14);
  const notes = str(formData.get("notes")) || null;
  const created_by = str(formData.get("created_by")) || null;

  if (!cohortChoice || !test_name) {
    throw new Error("cohort and test name are required");
  }

  const body: Parameters<typeof orchestrator.createCarePlan>[0] = {
    test_name,
    cadence_days,
    due_in_days,
    notes,
    created_by,
  };
  if (cohortChoice.startsWith("attr:")) {
    body.cohort_attr = cohortChoice.slice("attr:".length);
  } else if (cohortChoice.startsWith("tag:")) {
    body.cohort_tag_id = Number(cohortChoice.slice("tag:".length));
  } else {
    throw new Error(`unknown cohort encoding: ${cohortChoice}`);
  }

  await orchestrator.createCarePlan(body);
  revalidatePath("/care-plans");
  revalidatePath("/");
}

export async function updateCarePlanAction(formData: FormData) {
  const id = int(formData.get("plan_id"));
  await orchestrator.updateCarePlan(id, {
    cadence_days: int(formData.get("cadence_days")),
    due_in_days: int(formData.get("due_in_days"), 14),
    notes: str(formData.get("notes")) || null,
  });
  revalidatePath("/care-plans");
  revalidatePath("/");
}

export async function deactivateCarePlanAction(formData: FormData) {
  const id = int(formData.get("plan_id"));
  await orchestrator.deactivateCarePlan(id);
  revalidatePath("/care-plans");
  revalidatePath("/");
}

export async function reactivateCarePlanAction(formData: FormData) {
  const id = int(formData.get("plan_id"));
  await orchestrator.updateCarePlan(id, { active: true });
  revalidatePath("/care-plans");
  revalidatePath("/");
}
