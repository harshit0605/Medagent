"use client";

/**
 * Per-inbox-row reply form with an "AI draft" button.
 *
 * Replaces the previous server-only form so the operator can
 * click "AI draft" and have the textarea pre-fill with a
 * context-aware suggestion from the slice-13 drafter, then
 * edit + send via the existing server action.
 *
 * The draft button never sends — only the explicit "Send
 * reply" button does. Caveats from the LLM (low confidence,
 * open critical alert, etc.) render inline above the textarea
 * so the reviewer sees uncertainty before they edit.
 */

import { useState, useTransition } from "react";

import { orchestrator, type DraftReply } from "@/lib/backend";

import { sendDoctorReplyAction } from "../_actions";

type Props = {
  classificationId: number;
  patientDbId: number;
  inReplyToMessageId: string | null;
};

const CONFIDENCE_TONE: Record<DraftReply["confidence"], string> = {
  high: "text-emerald-700 dark:text-emerald-300",
  medium: "text-amber-700 dark:text-amber-300",
  low: "text-red-700 dark:text-red-300",
};

export function InboxReplyForm({
  classificationId,
  patientDbId,
  inReplyToMessageId,
}: Props) {
  const [body, setBody] = useState("");
  const [draftMeta, setDraftMeta] = useState<DraftReply | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [drafting, startDraft] = useTransition();

  const requestDraft = () => {
    setDraftError(null);
    startDraft(async () => {
      try {
        const result = await orchestrator.draftInboxReply(classificationId);
        setDraftMeta(result);
        // Always replace the textarea content — the operator
        // is asking for a fresh draft, so stomping their
        // unsent edits is the expected UX. (If they want to
        // preserve their typing they don't click Draft.)
        setBody(result.draft_text);
      } catch (err) {
        setDraftError(
          err instanceof Error ? err.message : String(err),
        );
      }
    });
  };

  return (
    <form className="mt-2 space-y-1">
      <input type="hidden" name="patient_id" value={patientDbId} />
      <input
        type="hidden"
        name="in_reply_to_message_id"
        value={inReplyToMessageId ?? ""}
      />
      <input type="hidden" name="sent_by" defaultValue="ops" />

      {/* Draft metadata banner — only renders after the
          operator clicks the AI draft button. Confidence +
          caveats give a "what to double-check" preview before
          they read the draft itself. */}
      {draftMeta ? (
        <div className="rounded border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-2 text-[11px] space-y-1">
          <div className="flex items-center gap-2">
            <span
              className={
                "font-medium uppercase tracking-wide " +
                CONFIDENCE_TONE[draftMeta.confidence]
              }
            >
              {draftMeta.confidence} confidence
            </span>
            <span className="text-zinc-400">·</span>
            <span className="font-mono text-zinc-500">
              {draftMeta.llm_model ?? "—"}
            </span>
          </div>
          {draftMeta.caveats.length > 0 ? (
            <ul className="list-disc list-inside space-y-0.5 text-zinc-700 dark:text-zinc-300">
              {draftMeta.caveats.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {draftError ? (
        <div className="rounded border border-red-300 bg-red-50 dark:bg-red-950/40 p-2 text-[11px] text-red-800 dark:text-red-200">
          AI draft failed: {draftError}
        </div>
      ) : null}

      <textarea
        name="body"
        rows={3}
        required
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Type your reply (sent freeform; only works inside the 24h CSW)"
        className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-xs"
      />
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={requestDraft}
          disabled={drafting}
          className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-xs disabled:opacity-50"
          title="Pre-fill with an AI-drafted suggestion based on the patient's recent context"
        >
          {drafting ? "Drafting…" : "✨ AI draft"}
        </button>
        <button
          type="submit"
          formAction={sendDoctorReplyAction}
          className="px-2 py-1 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white text-xs"
        >
          Send reply
        </button>
      </div>
    </form>
  );
}
