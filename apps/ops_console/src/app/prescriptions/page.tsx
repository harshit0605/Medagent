import Link from "next/link";

import { orchestrator, type Prescription } from "@/lib/backend";

export const dynamic = "force-dynamic";

const STATUS_TONE: Record<Prescription["status"], string> = {
  pending: "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  verified:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
  rejected: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
};

const CONFIDENCE_TONE: Record<string, string> = {
  high: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
  medium: "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  low: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
};

function formatDateTime(iso: string) {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default async function PrescriptionsPage() {
  let prescriptions: Prescription[] = [];
  let error: string | null = null;
  try {
    prescriptions = await orchestrator.listPrescriptions();
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  const pending = prescriptions.filter((r) => r.status === "pending");
  const others = prescriptions.filter((r) => r.status !== "pending");

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold">Prescriptions</h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Patients send prescription photos via WhatsApp. The vision LLM
          extracts medication orders here for clinician review — verified
          prescriptions auto-create regimens with reminders.
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t reach the orchestrator:{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : null}

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Pending review ({pending.length})
        </h2>
        {pending.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            None waiting. Patients can upload Rx photos via WhatsApp.
          </div>
        ) : (
          <div className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-4">
            {pending.map((rx) => (
              <PrescriptionCard key={rx.id} rx={rx} />
            ))}
          </div>
        )}
      </section>

      {others.length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Decided ({others.length})
          </h2>
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-900 text-xs uppercase tracking-wide text-zinc-500">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">Patient</th>
                  <th className="text-left px-4 py-2 font-medium">Status</th>
                  <th className="text-left px-4 py-2 font-medium">Decided</th>
                  <th className="text-left px-4 py-2 font-medium">By</th>
                  <th className="text-left px-4 py-2 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {others.map((rx) => (
                  <tr
                    key={rx.id}
                    className="border-t border-zinc-200 dark:border-zinc-800"
                  >
                    <td className="px-4 py-2">
                      {rx.patient_full_name ?? `#${rx.patient_id}`}
                    </td>
                    <td className="px-4 py-2">
                      <span
                        className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_TONE[rx.status]}`}
                      >
                        {rx.status}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-zinc-500">
                      {rx.verified_at ? formatDateTime(rx.verified_at) : "—"}
                    </td>
                    <td className="px-4 py-2 text-zinc-500">
                      {rx.verified_by ?? "—"}
                    </td>
                    <td className="px-4 py-2">
                      <Link
                        href={`/prescriptions/${rx.id}`}
                        className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                      >
                        View
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function PrescriptionCard({ rx }: { rx: Prescription }) {
  const meds = rx.parsed_regimens
    .map((r) => `${r.medication_name} ${r.dose}`)
    .slice(0, 4);
  return (
    <Link
      href={`/prescriptions/${rx.id}`}
      className="block rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 hover:border-zinc-300 dark:hover:border-zinc-700 overflow-hidden"
    >
      <div className="flex gap-4">
        {rx.public_path ? (
          <img
            src={rx.public_path}
            alt="prescription"
            className="w-32 h-32 object-cover bg-zinc-100 dark:bg-zinc-800"
          />
        ) : (
          <div className="w-32 h-32 bg-zinc-100 dark:bg-zinc-800 flex items-center justify-center text-xs text-zinc-400">
            no image
          </div>
        )}
        <div className="flex-1 p-3 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="font-medium truncate">
              {rx.patient_full_name ?? `Patient #${rx.patient_id}`}
            </div>
            {rx.confidence ? (
              <span
                className={`px-1.5 py-0.5 text-[10px] rounded ${CONFIDENCE_TONE[rx.confidence] ?? ""}`}
              >
                {rx.confidence}
              </span>
            ) : null}
            {rx.illegible ? (
              <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200">
                illegible
              </span>
            ) : null}
            {rx.vision_parse_failed ? (
              <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-200 dark:bg-zinc-700 text-zinc-700 dark:text-zinc-300">
                no auto-parse
              </span>
            ) : null}
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            uploaded {formatDateTime(rx.created_at)}
          </div>
          <div className="mt-2 text-xs text-zinc-700 dark:text-zinc-300">
            {meds.length > 0 ? (
              <ul className="space-y-0.5">
                {meds.map((m, i) => (
                  <li key={i} className="truncate">
                    • {m}
                  </li>
                ))}
                {rx.parsed_regimens.length > 4 ? (
                  <li className="text-zinc-400">
                    +{rx.parsed_regimens.length - 4} more…
                  </li>
                ) : null}
              </ul>
            ) : (
              <span className="text-zinc-400">no medications detected</span>
            )}
          </div>
          {rx.summary ? (
            <div className="mt-2 text-[11px] text-zinc-500 italic line-clamp-2">
              {rx.summary}
            </div>
          ) : null}
        </div>
      </div>
    </Link>
  );
}
