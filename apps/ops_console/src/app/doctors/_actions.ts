"use server";

import { revalidatePath } from "next/cache";

import { orchestrator } from "@/lib/backend";

export async function addDoctorAction(formData: FormData) {
  const name = String(formData.get("name") ?? "").trim();
  const email = String(formData.get("email") ?? "").trim();
  const phone = String(formData.get("phone") ?? "").trim() || null;
  const timezone = String(formData.get("timezone") ?? "UTC").trim() || "UTC";
  const calendar_id = String(formData.get("calendar_id") ?? "primary").trim() || "primary";

  if (!name || !email) {
    throw new Error("name and email are required");
  }

  await orchestrator.createDoctor({ name, email, phone, timezone, calendar_id });
  revalidatePath("/doctors");
}

export async function disconnectDoctorAction(formData: FormData) {
  const id = Number(formData.get("doctor_id"));
  if (!Number.isFinite(id)) throw new Error("invalid doctor_id");
  await orchestrator.disconnectDoctor(id);
  revalidatePath("/doctors");
}

export async function setDoctorOnCallAction(formData: FormData) {
  const id = Number(formData.get("doctor_id"));
  const onCall = formData.get("on_call") === "true";
  if (!Number.isFinite(id)) throw new Error("invalid doctor_id");
  await orchestrator.setDoctorOnCall(id, onCall);
  revalidatePath("/doctors");
}
