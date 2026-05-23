"use server";

import { revalidatePath } from "next/cache";

import { orchestrator } from "@/lib/backend";

function revalidateAll(ticketId: string) {
  revalidatePath("/tickets");
  revalidatePath(`/tickets/${ticketId}`);
  revalidatePath("/");
}

export async function ackTicketAction(ticketId: string, actor: string) {
  await orchestrator.ackTicket(ticketId, { actor });
  revalidateAll(ticketId);
}

export async function resolveTicketAction(
  ticketId: string,
  actor: string,
  notes?: string,
) {
  await orchestrator.resolveTicket(ticketId, { actor, notes });
  revalidateAll(ticketId);
}

// ---- Form-driven actions for the ticket detail page -----------------------

function fid(formData: FormData): { id: string; actor: string } {
  const id = String(formData.get("ticket_id") ?? "");
  if (!id) throw new Error("ticket_id is required");
  const actor = String(formData.get("actor") ?? "ops").trim() || "ops";
  return { id, actor };
}

export async function ackTicketFormAction(formData: FormData) {
  const { id, actor } = fid(formData);
  const notes = String(formData.get("notes") ?? "").trim() || undefined;
  await orchestrator.ackTicket(id, { actor, notes });
  revalidateAll(id);
}

export async function resolveTicketFormAction(formData: FormData) {
  const { id, actor } = fid(formData);
  const notes = String(formData.get("notes") ?? "").trim() || undefined;
  await orchestrator.resolveTicket(id, { actor, notes });
  revalidateAll(id);
}

export async function assignTicketAction(formData: FormData) {
  const { id, actor } = fid(formData);
  const assignedToRaw = String(formData.get("assigned_to") ?? "").trim();
  const notes = String(formData.get("notes") ?? "").trim() || undefined;
  await orchestrator.assignTicket(id, {
    actor,
    assigned_to: assignedToRaw || null,
    notes,
  });
  revalidateAll(id);
}

export async function snoozeTicketAction(formData: FormData) {
  const { id, actor } = fid(formData);
  const minutesRaw = String(formData.get("minutes") ?? "").trim();
  const minutes = minutesRaw ? Number(minutesRaw) : 60;
  if (!Number.isFinite(minutes) || minutes < 1) {
    throw new Error("snoozeTicketAction: minutes must be a positive integer");
  }
  const notes = String(formData.get("notes") ?? "").trim() || undefined;
  await orchestrator.snoozeTicket(id, { actor, minutes, notes });
  revalidateAll(id);
}

export async function unsnoozeTicketAction(formData: FormData) {
  const { id, actor } = fid(formData);
  await orchestrator.unsnoozeTicket(id, { actor });
  revalidateAll(id);
}

export async function reopenTicketAction(formData: FormData) {
  const { id, actor } = fid(formData);
  const notes = String(formData.get("notes") ?? "").trim() || undefined;
  await orchestrator.reopenTicket(id, { actor, notes });
  revalidateAll(id);
}

export async function addTicketNoteAction(formData: FormData) {
  const { id, actor } = fid(formData);
  const note = String(formData.get("note") ?? "").trim();
  if (!note) {
    throw new Error("addTicketNoteAction: note is required");
  }
  await orchestrator.addTicketNote(id, { actor, note });
  revalidateAll(id);
}
