"use server";

import { orchestrator, type RouteResponse } from "@/lib/backend";

export type RouteActionState = {
  ok: boolean;
  result?: RouteResponse;
  error?: string;
  request?: { patient_id: string; text: string };
};

export async function routeAction(
  _prev: RouteActionState | null,
  formData: FormData,
): Promise<RouteActionState> {
  const patient_id = String(formData.get("patient_id") ?? "").trim();
  const text = String(formData.get("text") ?? "").trim();
  const phone = (formData.get("phone") as string | null)?.trim() || undefined;

  if (!patient_id || !text) {
    return { ok: false, error: "patient_id and text are required" };
  }

  const message_id = `console-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  try {
    const result = await orchestrator.route({
      message: { message_id, patient_id, text, phone: phone ?? null },
    });
    return { ok: true, result, request: { patient_id, text } };
  } catch (err) {
    return {
      ok: false,
      error: err instanceof Error ? err.message : String(err),
      request: { patient_id, text },
    };
  }
}
