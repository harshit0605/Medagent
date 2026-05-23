"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { orchestrator } from "@/lib/backend";

function str(value: FormDataEntryValue | null): string {
  return String(value ?? "").trim();
}

export async function createBroadcastCampaignAction(formData: FormData) {
  const name = str(formData.get("name"));
  const templateName = str(formData.get("template_name"));
  const cohort = str(formData.get("cohort"));
  const createdBy = str(formData.get("created_by")) || "ops";
  const notes = str(formData.get("notes")) || undefined;

  if (!name || !templateName) {
    throw new Error("name and template_name are required");
  }
  if (!["diabetes", "cardiac", "fall_risk"].includes(cohort)) {
    throw new Error("cohort must be diabetes / cardiac / fall_risk");
  }

  // Optional template_params parsing — we accept a JSON blob
  // pasted into the form as a power-user shortcut. Empty input
  // → no params (template carries no body slots).
  const paramsRaw = str(formData.get("template_params"));
  let templateParams: Record<string, string> = {};
  if (paramsRaw) {
    try {
      const parsed = JSON.parse(paramsRaw);
      if (parsed && typeof parsed === "object") {
        templateParams = Object.fromEntries(
          Object.entries(parsed).map(([k, v]) => [k, String(v)]),
        );
      }
    } catch (err) {
      throw new Error(
        `template_params is not valid JSON: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
    }
  }

  const result = await orchestrator.createBroadcastCampaign({
    name,
    template_name: templateName,
    template_params: templateParams,
    cohort_filter: {
      cohort: cohort as "diabetes" | "cardiac" | "fall_risk",
    },
    created_by: createdBy,
    notes,
    materialise_immediately: true,
  });

  // Land the operator on the new campaign's detail page so they
  // see the materialisation outcome (counts + skip reasons)
  // immediately. revalidatePath is needed so the list page shows
  // the row on next visit.
  revalidatePath("/campaigns");
  redirect(`/campaigns/${result.campaign.id}`);
}
