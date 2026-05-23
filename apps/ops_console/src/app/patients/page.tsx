import Link from "next/link";

import { orchestrator, type PatientSummary } from "@/lib/backend";

export const dynamic = "force-dynamic";

function CohortBadge({ label, on }: { label: string; on: boolean }) {
  if (!on) return null;
  return (
    <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
      {label}
    </span>
  );
}

function CountChip({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: number;
  tone?: "neutral" | "warn";
}) {
  const palette =
    tone === "warn" && value > 0
      ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-200"
      : value > 0
        ? "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
        : "bg-transparent text-zinc-400";
  return (
    <span className={`px-2 py-0.5 text-xs rounded ${palette}`}>
      {value} {label}
    </span>
  );
}

export default async function PatientsPage() {
  let patients: PatientSummary[] = [];
  let error: string | null = null;
  try {
    patients = await orchestrator.listPatients();
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Patients</h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Roster of patients who have interacted with the bot. Click a row to
          view regimens, adherence, and upcoming appointments.
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t reach the orchestrator:{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : null}

      {patients.length === 0 && !error ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
          No patients yet. They&apos;re auto-created on first inbound WhatsApp
          message.
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 dark:bg-zinc-900 text-xs uppercase tracking-wide text-zinc-500">
              <tr>
                <th className="text-left px-4 py-2 font-medium">Patient</th>
                <th className="text-left px-4 py-2 font-medium">Phone</th>
                <th className="text-left px-4 py-2 font-medium">Cohorts</th>
                <th className="text-left px-4 py-2 font-medium">Activity</th>
              </tr>
            </thead>
            <tbody>
              {patients.map((p) => (
                <tr
                  key={p.id}
                  className="border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-900/40"
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/patients/${p.id}`}
                      className="font-medium text-blue-600 hover:underline dark:text-blue-400"
                    >
                      {p.full_name}
                    </Link>
                    <div className="text-[11px] text-zinc-400">id={p.id}</div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{p.phone}</td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      <CohortBadge label="diabetes" on={p.cohort_diabetes} />
                      <CohortBadge label="cardiac" on={p.cohort_cardiac} />
                      <CohortBadge label="fall-risk" on={p.cohort_fall_risk} />
                      {!p.cohort_diabetes &&
                        !p.cohort_cardiac &&
                        !p.cohort_fall_risk && (
                          <span className="text-[11px] text-zinc-400">—</span>
                        )}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-2">
                      <CountChip
                        label="regimens"
                        value={p.active_regimen_count}
                      />
                      <CountChip
                        label="upcoming appts"
                        value={p.upcoming_appointment_count}
                      />
                      <CountChip
                        label="open tickets"
                        value={p.open_ticket_count}
                        tone="warn"
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
