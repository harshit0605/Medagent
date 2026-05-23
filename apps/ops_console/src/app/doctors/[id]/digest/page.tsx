import Link from "next/link";

import { orchestrator } from "@/lib/backend";

export const dynamic = "force-dynamic";

const PRIORITY_BADGE: Record<string, string> = {
  p0: "bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-200",
  p1: "bg-orange-100 text-orange-800 dark:bg-orange-950/60 dark:text-orange-200",
  critical: "bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-200",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-950/60 dark:text-orange-200",
};

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const diff = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(diff);
  const tail = diff >= 0 ? " ago" : "";
  if (abs < 60) return `${abs}s${tail}`;
  if (abs < 3600) return `${Math.round(abs / 60)}m${tail}`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h${tail}`;
  return `${Math.round(abs / 86400)}d${tail}`;
}

export default async function DoctorDigestPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const doctorId = Number(id);
  if (Number.isNaN(doctorId)) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
        Invalid doctor id: <code>{id}</code>
      </div>
    );
  }

  let digest;
  let error: string | null = null;
  try {
    digest = await orchestrator.getDoctorDailyDigest(doctorId);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !digest) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load the digest.{" "}
        <code className="font-mono text-xs">{error}</code>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">
            {digest.doctor_name}&apos;s daily digest
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            {formatDate(digest.when)} · panel of{" "}
            {digest.summary_counts.panel_size} patient(s) (last 90
            days)
          </p>
        </div>
        <Link
          href="/doctors"
          className="text-xs text-zinc-500 hover:underline"
        >
          ← all doctors
        </Link>
      </div>

      {/* Top-line counters — 3-second skim. Each tile click-
          throughs to the section below it. */}
      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryTile
          label="Today"
          value={digest.summary_counts.appointments_today}
          hint="appointments scheduled"
        />
        <SummaryTile
          label="Recap drafts"
          value={digest.summary_counts.recap_drafts_pending}
          hint="awaiting your send"
          tone={
            digest.summary_counts.recap_drafts_pending > 0
              ? "amber"
              : undefined
          }
        />
        <SummaryTile
          label="Side-effect reports"
          value={digest.summary_counts.side_effect_reports_24h}
          hint="last 24h"
          tone={
            digest.summary_counts.side_effect_reports_24h > 0
              ? "red"
              : undefined
          }
        />
        <SummaryTile
          label="Open high-pri tickets"
          value={digest.summary_counts.open_tickets}
          hint="across panel"
          tone={
            digest.summary_counts.open_tickets > 0
              ? "amber"
              : undefined
          }
        />
      </section>

      {/* Side-effect reports first — patient-safety priority. A
          doctor unaware of an overnight reaction is the failure
          mode this section catches. */}
      {digest.side_effect_reports_24h.length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-red-700 dark:text-red-300 uppercase tracking-wide flex items-center gap-2">
            <span>⚠️ Side-effect reports (last 24h)</span>
          </h2>
          <div className="mt-3 space-y-2">
            {digest.side_effect_reports_24h.map((report) => (
              <div
                key={report.ticket_id}
                className={
                  "rounded-lg border p-3 text-sm " +
                  (report.status === "resolved"
                    ? "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900"
                    : "border-red-200 dark:border-red-900/50 bg-red-50/50 dark:bg-red-950/20")
                }
              >
                <div className="flex items-baseline justify-between gap-3 flex-wrap">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-zinc-500">
                      {formatRelative(report.created_at)}
                    </span>
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200">
                      {report.status}
                    </span>
                    {report.patient_full_name &&
                    report.patient_id ? (
                      <Link
                        href={`/patients/${report.patient_id}`}
                        className="text-blue-600 dark:text-blue-400 hover:underline font-medium"
                      >
                        {report.patient_full_name}
                      </Link>
                    ) : null}
                  </div>
                  <Link
                    href={`/tickets/${report.ticket_id}`}
                    className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                  >
                    ticket #{report.ticket_id} →
                  </Link>
                </div>
                {report.reported_text ? (
                  <blockquote className="mt-2 pl-3 border-l-2 border-red-300 dark:border-red-800 text-zinc-700 dark:text-zinc-300 italic whitespace-pre-line">
                    {report.reported_text}
                  </blockquote>
                ) : null}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {/* Today's appointments. */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Today&apos;s schedule
        </h2>
        {digest.appointments_today.length === 0 ? (
          <div className="mt-2 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            No appointments scheduled today.
          </div>
        ) : (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
                <tr>
                  <th className="text-left px-3 py-2 w-24">Time</th>
                  <th className="text-left px-3 py-2">Patient</th>
                  <th className="text-left px-3 py-2">Status</th>
                  <th className="text-left px-3 py-2">Summary</th>
                  <th className="text-left px-3 py-2 w-28">Pre-visit</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {digest.appointments_today.map((appt) => (
                  <tr key={appt.appointment_id}>
                    <td className="px-3 py-2 align-top text-xs whitespace-nowrap">
                      {formatTime(appt.scheduled_for)} –{" "}
                      {formatTime(appt.end_at)}
                    </td>
                    <td className="px-3 py-2 align-top">
                      <Link
                        href={`/patients/${appt.patient_id}`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {appt.patient_full_name}
                      </Link>
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      <span
                        className={
                          "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                          (appt.status === "confirmed"
                            ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
                            : appt.status === "cancelled"
                              ? "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                              : "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200")
                        }
                      >
                        {appt.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 align-top text-xs text-zinc-600 dark:text-zinc-300">
                      {appt.summary ?? (
                        <span className="text-zinc-400">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      <Link
                        href={`/appointments/${appt.appointment_id}/pre-visit`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        brief →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Recap drafts the doctor authored but hasn't sent yet. */}
      {digest.recap_drafts_pending.length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-amber-700 dark:text-amber-300 uppercase tracking-wide">
            Recap drafts pending your send
          </h2>
          <div className="mt-3 rounded-lg border border-amber-200 dark:border-amber-900/40 bg-amber-50/40 dark:bg-amber-950/10 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-amber-100/30 dark:bg-amber-950/40 text-xs uppercase text-amber-700 dark:text-amber-300">
                <tr>
                  <th className="text-left px-3 py-2">Patient</th>
                  <th className="text-left px-3 py-2">
                    Appointment date
                  </th>
                  <th className="text-left px-3 py-2">Drafted</th>
                  <th className="text-left px-3 py-2 w-28">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-amber-200 dark:divide-amber-900/40">
                {digest.recap_drafts_pending.map((draft) => (
                  <tr key={draft.recap_id}>
                    <td className="px-3 py-2 align-top">
                      <Link
                        href={`/patients/${draft.patient_id}`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {draft.patient_full_name}
                      </Link>
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      {formatDate(draft.appointment_date)}
                    </td>
                    <td className="px-3 py-2 align-top text-xs text-zinc-500">
                      {formatRelative(draft.created_at)}
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      <Link
                        href={`/appointments/${draft.appointment_id}/recap`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        review →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {/* High-priority tickets across panel. */}
      {digest.open_tickets.length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Open high-priority tickets (panel)
          </h2>
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
                <tr>
                  <th className="text-left px-3 py-2">Patient</th>
                  <th className="text-left px-3 py-2">Category</th>
                  <th className="text-left px-3 py-2">Priority</th>
                  <th className="text-left px-3 py-2">Status</th>
                  <th className="text-left px-3 py-2">Created</th>
                  <th className="text-left px-3 py-2 w-28">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {digest.open_tickets.map((ticket) => (
                  <tr
                    key={ticket.ticket_id}
                    className={
                      ticket.sla_breached_at
                        ? "bg-red-50/30 dark:bg-red-950/10"
                        : ""
                    }
                  >
                    <td className="px-3 py-2 align-top">
                      {ticket.patient_id !== null &&
                      ticket.patient_full_name ? (
                        <Link
                          href={`/patients/${ticket.patient_id}`}
                          className="text-blue-600 hover:underline dark:text-blue-400"
                        >
                          {ticket.patient_full_name}
                        </Link>
                      ) : (
                        <span className="font-mono text-xs">
                          {ticket.patient_phone}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      {ticket.category}
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      <span
                        className={
                          "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                          (PRIORITY_BADGE[ticket.priority] ??
                            "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300")
                        }
                      >
                        {ticket.priority}
                      </span>
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      {ticket.status}
                    </td>
                    <td className="px-3 py-2 align-top text-xs text-zinc-500">
                      {formatRelative(ticket.created_at)}
                    </td>
                    <td className="px-3 py-2 align-top text-xs">
                      <Link
                        href={`/tickets/${ticket.ticket_id}`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        ticket →
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

function SummaryTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: number;
  hint: string;
  tone?: "amber" | "red";
}) {
  const valueClass =
    tone === "red"
      ? "text-red-700 dark:text-red-300"
      : tone === "amber"
        ? "text-amber-700 dark:text-amber-300"
        : "";
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className={`text-2xl font-semibold ${valueClass}`}>
        {value}
      </div>
      <div className="text-xs text-zinc-500 mt-1">{label}</div>
      <div className="text-[10px] text-zinc-400 mt-0.5">{hint}</div>
    </div>
  );
}
