"use client";

/**
 * DSAR right-of-access export button.
 *
 * Client component because the file download has to happen in the
 * browser — we fetch the JSON document from the orchestrator,
 * convert it to a Blob, generate a temporary object-URL, and
 * trigger a click on a hidden anchor with a Content-Disposition
 * filename. A pure server-action flow can't initiate a
 * client-side download.
 *
 * Filename embeds patient id + ISO date so an ops user with
 * multiple exports doesn't collide. Errors render inline below
 * the button rather than throwing — the export is high-stakes
 * and the operator needs to see exactly what failed.
 */

import { useState, useTransition } from "react";

import { orchestrator } from "@/lib/backend";

type Props = {
  patientId: number;
  patientFullName: string;
};

const DEFAULT_ACTOR = "ops_console";
const DEFAULT_WINDOW_DAYS = 365;

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function safeFilenamePart(value: string): string {
  // Filename character set: keep alphanumerics + dash/underscore.
  // Patient names with spaces / punctuation would otherwise produce
  // weird Content-Disposition filenames across browsers.
  return value
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

export function ExportPatientButton({ patientId, patientFullName }: Props) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const onExport = () => {
    setError(null);
    startTransition(async () => {
      try {
        const document = await orchestrator.exportPatient(patientId, {
          actor: DEFAULT_ACTOR,
          window_days: DEFAULT_WINDOW_DAYS,
        });
        const json = JSON.stringify(document, null, 2);
        const blob = new Blob([json], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const namePart = safeFilenamePart(patientFullName) || `patient-${patientId}`;
        const filename = `dsar-${namePart}-${patientId}-${todayIso()}.json`;
        const a = window.document.createElement("a");
        a.href = url;
        a.download = filename;
        // Append-then-remove pattern so the click works in older
        // Safari versions which require the anchor to be in the DOM.
        window.document.body.appendChild(a);
        a.click();
        window.document.body.removeChild(a);
        // Slight delay before revoking the object URL — Safari has
        // historically dropped the download if the URL is revoked
        // synchronously.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  };

  return (
    <div className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        onClick={onExport}
        disabled={pending}
        className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50"
        title="Download a JSON copy of this patient's data (DSAR right-of-access)"
      >
        {pending ? "Exporting…" : "Export patient data"}
      </button>
      {error ? (
        <div className="text-[11px] text-red-600 dark:text-red-400 max-w-xs">
          Export failed: {error}
        </div>
      ) : null}
    </div>
  );
}
