"use client";

/**
 * Right-of-erasure trigger.
 *
 * Irreversible operation — once submitted, the patient row's PII
 * is overwritten in place and cannot be recovered. The UI must
 * guard against accidental clicks aggressively:
 *
 *   1. Click "Erase patient data" → opens an inline confirm panel
 *      (not just a button → instant destructive action).
 *   2. The confirm panel requires the operator to:
 *      - type the patient's full name to confirm they know which
 *        record they're erasing
 *      - provide a reason (regulator audit trail requires it)
 *      - tick a "I understand this is irreversible" checkbox
 *   3. Submit → POST /patients/{id}/erase with confirm=true.
 *
 * After successful erasure the page reloads to render the post-
 * erasure state (banner + anonymized values).
 */

import { useState, useTransition } from "react";

import { erasePatientAction } from "../_actions";

type Props = {
  patientId: number;
  patientFullName: string;
};

export function ErasePatientButton({ patientId, patientFullName }: Props) {
  const [open, setOpen] = useState(false);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const [typedName, setTypedName] = useState("");
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);

  const canSubmit =
    typedName === patientFullName &&
    reason.trim().length > 0 &&
    acknowledged &&
    !pending;

  const onSubmit = () => {
    if (!canSubmit) return;
    setError(null);
    startTransition(async () => {
      try {
        await erasePatientAction(patientId, reason.trim());
        // Hard reload — the page renders against the freshly
        // anonymized data without us having to thread state.
        window.location.reload();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="px-3 py-1 text-xs rounded border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40"
        title="Right-of-erasure: irreversibly anonymize this patient's data per GDPR / DPDP."
      >
        🗑 Erase patient data
      </button>
    );
  }

  return (
    <div className="rounded-lg border-2 border-red-400 dark:border-red-700 bg-red-50 dark:bg-red-950/40 p-3 max-w-md">
      <div className="text-sm font-semibold text-red-800 dark:text-red-200">
        Confirm erasure
      </div>
      <p className="mt-1 text-xs text-red-700 dark:text-red-300">
        This will <strong>irreversibly</strong> overwrite the
        patient&apos;s PII (name, phone, message log, ticket notes,
        caregiver records). Clinical history (regimens, adherence,
        labs, recaps) is retained but de-identified. Used to satisfy
        GDPR Art. 17 / India DPDP §13 erasure requests.
      </p>

      <label className="block mt-3 text-xs">
        <span className="block text-red-700 dark:text-red-300 mb-1">
          Type the patient&apos;s full name to confirm:{" "}
          <code>{patientFullName}</code>
        </span>
        <input
          type="text"
          value={typedName}
          onChange={(e) => setTypedName(e.target.value)}
          className="w-full rounded-md border border-red-300 dark:border-red-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
        />
      </label>

      <label className="block mt-2 text-xs">
        <span className="block text-red-700 dark:text-red-300 mb-1">
          Reason (required — captured in audit log):
        </span>
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. patient SAR request 2026-05-08"
          className="w-full rounded-md border border-red-300 dark:border-red-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
        />
      </label>

      <label className="flex items-start gap-2 mt-3 text-xs text-red-700 dark:text-red-300">
        <input
          type="checkbox"
          checked={acknowledged}
          onChange={(e) => setAcknowledged(e.target.checked)}
          className="mt-0.5"
        />
        <span>
          I understand this is irreversible and the patient&apos;s
          PII cannot be recovered.
        </span>
      </label>

      {error ? (
        <div className="mt-2 text-xs text-red-700 dark:text-red-300 font-medium">
          Erasure failed: {error}
        </div>
      ) : null}

      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={onSubmit}
          disabled={!canSubmit}
          className="px-3 py-1.5 text-xs rounded bg-red-600 hover:bg-red-700 text-white disabled:bg-red-300 disabled:cursor-not-allowed"
        >
          {pending ? "Erasing…" : "Confirm erasure"}
        </button>
        <button
          type="button"
          onClick={() => {
            setOpen(false);
            setTypedName("");
            setReason("");
            setAcknowledged(false);
            setError(null);
          }}
          className="px-3 py-1.5 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
