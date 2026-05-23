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

export async function createCohortTagAction(formData: FormData) {
  const label = str(formData.get("label"));
  if (!label) throw new Error("label is required");
  await orchestrator.createCohortTag({
    label,
    slug: str(formData.get("slug")) || null,
    description: str(formData.get("description")) || null,
    created_by: str(formData.get("created_by")) || null,
  });
  revalidatePath("/cohort-tags");
  revalidatePath("/care-plans");
}

export async function updateCohortTagAction(formData: FormData) {
  const id = int(formData.get("tag_id"));
  await orchestrator.updateCohortTag(id, {
    label: str(formData.get("label")) || undefined,
    description: str(formData.get("description")) || null,
  });
  revalidatePath("/cohort-tags");
  revalidatePath("/care-plans");
}

export async function deactivateCohortTagAction(formData: FormData) {
  const id = int(formData.get("tag_id"));
  await orchestrator.updateCohortTag(id, { active: false });
  revalidatePath("/cohort-tags");
  revalidatePath("/care-plans");
}

export async function reactivateCohortTagAction(formData: FormData) {
  const id = int(formData.get("tag_id"));
  await orchestrator.updateCohortTag(id, { active: true });
  revalidatePath("/cohort-tags");
  revalidatePath("/care-plans");
}
