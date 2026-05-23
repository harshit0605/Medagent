import Link from "next/link";
import { notFound } from "next/navigation";

import { orchestrator, type Prescription } from "@/lib/backend";

import {
  rejectPrescriptionAction,
  verifyPrescriptionAction,
} from "../_actions";

export const dynamic = "force-dynamic";

const STATUS_TONE: Record<Prescription["status"], string> = {
  pending: "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  verified:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
  rejected: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
};

function formatDateTime(iso: string) {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default async function PrescriptionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const prescriptionId = Number(id);
  if (!prescriptionId || Number.isNaN(prescriptionId)) notFound();

  let rx: Prescription | null = null;
  let error: string | null = null;
  try {
    rx = await orchestrator.getPrescription(prescriptionId);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }
  if (error && /404/.test(error)) notFound();
  if (!rx) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load prescription {prescriptionId}:{" "}
        <code className="font-mono text-xs">{error ?? "unknown"}</code>
      </div>
    );
  }

  // Pad parsed_regimens with one blank row at the end so clinicians can
  // add a regimen the LLM missed without needing a separate "add row"
  // button. Empty rows are dropped server-side.
  const rows = [
    ...rx.parsed_regimens,
    { medication_name: "", dose: "", times_of_day: [] as string[] },
  ];
  const isPending = rx.status === "pending";

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/prescriptions"
          className="text-xs text-zinc-500 hover:underline"
        >
          ← back to prescriptions
        </Link>
        <div className="mt-1 flex items-center gap-2 flex-wrap">
          <h1 className="text-xl font-semibold">
            Prescription #{rx.id} ·{" "}
            <Link
              href={`/patients/${rx.patient_id}`}
              className="text-blue-600 hover:underline dark:text-blue-400"
            >
              {rx.patient_full_name ?? `Patient #${rx.patient_id}`}
            </Link>
          </h1>
          <span
            className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_TONE[rx.status]}`}
          >
            {rx.status}
          </span>
          {rx.confidence ? (
            <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-100 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-300">
              confidence: {rx.confidence}
            </span>
          ) : null}
          {rx.illegible ? (
            <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200">
              illegible
            </span>
          ) : null}
        </div>
        <div className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          uploaded {formatDateTime(rx.created_at)}
          {rx.verified_at && rx.verified_by ? (
            <>
              {" · "}
              {rx.status} by {rx.verified_by} at {formatDateTime(rx.verified_at)}
            </>
          ) : null}
        </div>
        {rx.summary ? (
          <div className="mt-2 text-sm text-zinc-600 dark:text-zinc-400 italic">
            {rx.summary}
          </div>
        ) : null}
      </div>

      {/* Image preview */}
      <section>
        {rx.public_path ? (
          <img
            src={rx.public_path}
            alt="prescription"
            className="max-w-full rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-100 dark:bg-zinc-800"
            style={{ maxHeight: "70vh" }}
          />
        ) : (
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-sm text-zinc-500">
            Image source unavailable.
          </div>
        )}
      </section>

      {/* Regimen review form */}
      {isPending ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Review &amp; verify
          </h2>
          <p className="mt-1 text-xs text-zinc-500">
            Edit the parsed regimens below — what you confirm is what gets
            created. Empty rows (no medication or dose) are dropped on save.
            Times of day are <code>HH:MM</code> in the patient&apos;s timezone,
            comma-separated.
          </p>
          <form
            action={verifyPrescriptionAction}
            className="mt-3 space-y-4"
          >
            <input
              type="hidden"
              name="prescription_id"
              value={rx.id}
            />
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="flex flex-col gap-1 text-xs">
                Verified by
                <input
                  name="verified_by"
                  defaultValue="ops"
                  required
                  className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                />
              </label>
              <label className="flex flex-col gap-1 text-xs">
                Timezone (for all regimens)
                <input
                  name="timezone"
                  defaultValue="Asia/Kolkata"
                  className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                />
              </label>
            </div>

            {rows.map((r, i) => (
              <div
                key={i}
                className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-3"
              >
                <div className="text-xs uppercase tracking-wide text-zinc-500 mb-2">
                  Regimen {i + 1}
                  {i === rows.length - 1 ? (
                    <span className="ml-2 text-[10px] text-zinc-400 normal-case">
                      (blank — fill to add a new one)
                    </span>
                  ) : null}
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                  <label className="flex flex-col gap-1 text-xs">
                    Medication
                    <input
                      name={`medication_name_${i}`}
                      defaultValue={r.medication_name}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                      placeholder="Metformin"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    Dose
                    <input
                      name={`dose_${i}`}
                      defaultValue={r.dose}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                      placeholder="500 mg"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs lg:col-span-1">
                    Times of day (HH:MM, comma-sep)
                    <input
                      name={`times_of_day_${i}`}
                      defaultValue={r.times_of_day.join(", ")}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                      placeholder="08:00, 20:00"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    Duration (days, optional)
                    <input
                      name={`duration_days_${i}`}
                      type="number"
                      min={1}
                      max={365}
                      defaultValue={r.duration_days ?? ""}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                      placeholder="30"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs sm:col-span-2 lg:col-span-2">
                    Frequency (as written on Rx)
                    <input
                      name={`frequency_text_${i}`}
                      defaultValue={r.frequency_text ?? ""}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                      placeholder="twice daily"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs sm:col-span-2 lg:col-span-2">
                    Notes
                    <input
                      name={`notes_${i}`}
                      defaultValue={r.notes ?? ""}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                      placeholder="with food"
                    />
                  </label>
                </div>
              </div>
            ))}

            <div className="flex items-center gap-3 flex-wrap">
              <button
                type="submit"
                className="px-4 py-2 rounded bg-emerald-600 text-white hover:bg-emerald-500"
              >
                Verify &amp; create regimens
              </button>
            </div>
          </form>

          <h3 className="mt-8 text-xs uppercase tracking-wide text-zinc-500">
            Or reject
          </h3>
          <form
            action={rejectPrescriptionAction}
            className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-3 items-end"
          >
            <input type="hidden" name="prescription_id" value={rx.id} />
            <label className="flex flex-col gap-1 text-xs">
              Rejected by
              <input
                name="rejected_by"
                defaultValue="ops"
                className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs sm:col-span-1">
              Reason (optional)
              <input
                name="reason"
                placeholder="image too blurry — ask patient to re-upload"
                className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
              />
            </label>
            <button
              type="submit"
              className="px-4 py-2 rounded border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/40"
            >
              Reject
            </button>
          </form>
        </section>
      ) : (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Final regimen list
          </h2>
          {rx.parsed_regimens.length === 0 ? (
            <div className="mt-3 text-sm text-zinc-500">
              No regimens recorded.
            </div>
          ) : (
            <ul className="mt-3 space-y-1 text-sm">
              {rx.parsed_regimens.map((r, i) => (
                <li key={i}>
                  • <span className="font-medium">{r.medication_name}</span>{" "}
                  <span className="text-zinc-500">{r.dose}</span>
                  {r.times_of_day.length > 0 ? (
                    <span className="text-zinc-500">
                      {" "}
                      at {r.times_of_day.join(", ")}
                    </span>
                  ) : null}
                  {r.frequency_text ? (
                    <span className="text-zinc-500">
                      {" "}
                      ({r.frequency_text})
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
