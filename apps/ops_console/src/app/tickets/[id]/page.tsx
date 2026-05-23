import Link from "next/link";
import { notFound } from "next/navigation";

import {
  orchestrator,
  type OpsTicket,
  type PatientDetail,
} from "@/lib/backend";

import {
  ackTicketFormAction,
  addTicketNoteAction,
  assignTicketAction,
  reopenTicketAction,
  resolveTicketFormAction,
  snoozeTicketAction,
  unsnoozeTicketAction,
} from "../_actions";

export const dynamic = "force-dynamic";

const PRIORITY_BADGE: Record<string, string> = {
  p0: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  p1: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200",
  p2: "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-200",
  p3: "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
};

const STATUS_BADGE: Record<string, string> = {
  open: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  acknowledged: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  resolved: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
};

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const diffSec = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(diffSec);
  const sign = diffSec >= 0 ? "" : "in ";
  const tail = diffSec >= 0 ? " ago" : "";
  if (abs < 60) return `${sign}${abs}s${tail}`;
  if (abs < 3600) return `${sign}${Math.round(abs / 60)}m${tail}`;
  if (abs < 86400) return `${sign}${Math.round(abs / 3600)}h${tail}`;
  return `${sign}${Math.round(abs / 86400)}d${tail}`;
}

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

export default async function TicketDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  if (!id) notFound();

  let ticket: OpsTicket | null = null;
  let error: string | null = null;
  try {
    ticket = await orchestrator.getTicket(id);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }
  if (error && /404/.test(error)) notFound();
  if (!ticket) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load ticket {id}:{" "}
        <code className="font-mono text-xs">{error ?? "unknown"}</code>
      </div>
    );
  }

  // Fetch patient context (best-effort).
  let patient: PatientDetail | null = null;
  if (ticket.patient_db_id !== null) {
    try {
      patient = await orchestrator.getPatient(ticket.patient_db_id);
    } catch {
      patient = null;
    }
  }

  const isResolved = ticket.status === "resolved";
  const isOpen = ticket.status === "open";
  const lowSupplyRegimens = (patient?.regimens ?? []).filter(
    (r) =>
      r.days_of_supply_remaining !== null &&
      r.days_of_supply_remaining <= 7 &&
      (r.ends_on === null || new Date(r.ends_on) >= new Date()),
  );
  const adhRatePct = Math.round(
    (patient?.adherence_summary.adherence_rate ?? 0) * 100,
  );

  return (
    <div className="space-y-6">
      <div>
        <Link href="/tickets" className="text-xs text-zinc-500 hover:underline">
          ← back to tickets
        </Link>
        <div className="mt-1 flex items-center gap-2 flex-wrap">
          <h1 className="text-xl font-semibold">Ticket #{ticket.ticket_id}</h1>
          <span
            className={
              "px-2 py-0.5 rounded text-xs font-medium " +
              (STATUS_BADGE[ticket.status] ?? "")
            }
          >
            {ticket.status}
          </span>
          <span
            className={
              "px-2 py-0.5 rounded text-xs font-medium " +
              (PRIORITY_BADGE[ticket.priority] ?? PRIORITY_BADGE.p3)
            }
          >
            {ticket.priority}
          </span>
          <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
            {ticket.category}
          </span>
          {ticket.is_snoozed && ticket.snoozed_until ? (
            <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
              snoozed · resumes {formatRelative(ticket.snoozed_until)}
            </span>
          ) : ticket.is_overdue ? (
            <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200 font-medium">
              SLA overdue
            </span>
          ) : null}
        </div>
        <div className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          opened {formatDateTime(ticket.created_at)} · SLA {ticket.sla_minutes}m{" "}
          (due {formatDateTime(ticket.sla_due_at)})
          {ticket.assigned_to ? <> · assigned to <strong>{ticket.assigned_to}</strong></> : <> · unassigned</>}
        </div>
      </div>

      {/* Patient context panel */}
      {patient ? (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div>
              <Link
                href={`/patients/${patient.id}`}
                className="font-medium text-blue-600 hover:underline dark:text-blue-400"
              >
                {patient.full_name}
              </Link>
              <span className="ml-2 font-mono text-xs text-zinc-500">
                {patient.phone}
              </span>
            </div>
            <div className="flex gap-3 text-xs text-zinc-600 dark:text-zinc-400">
              <span>
                <strong>{adhRatePct}%</strong> adherence (30d)
              </span>
              <span>
                <strong>{patient.adherence_summary.missed}</strong> missed doses
              </span>
              <span>
                <strong>{patient.upcoming_appointments.length}</strong> upcoming
                appts
              </span>
            </div>
          </div>
          {lowSupplyRegimens.length > 0 ? (
            <div className="mt-2 text-xs text-amber-700 dark:text-amber-400">
              ⚠️ Running low:{" "}
              {lowSupplyRegimens
                .map(
                  (r) => `${r.medication_name} (${r.days_of_supply_remaining}d)`,
                )
                .join(", ")}
            </div>
          ) : null}
        </section>
      ) : ticket.patient_db_id === null ? (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 text-sm text-zinc-500">
          Patient row not found for{" "}
          <code className="font-mono text-xs">{ticket.patient_id}</code>.
        </section>
      ) : null}

      {/* Notes / activity log */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Activity &amp; notes
        </h2>
        <pre className="mt-2 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3 text-xs text-zinc-700 dark:text-zinc-300 whitespace-pre-wrap font-mono overflow-x-auto">
          {ticket.notes ?? "(no notes yet)"}
        </pre>
        <form
          action={addTicketNoteAction}
          className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 items-end"
        >
          <input type="hidden" name="ticket_id" value={ticket.ticket_id} />
          <label className="flex flex-col gap-1 text-xs">
            Actor
            <input
              name="actor"
              defaultValue="ops"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs sm:col-span-1">
            Add note
            <input
              name="note"
              required
              placeholder="Called patient at 11:00 — left voicemail"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <button
            type="submit"
            className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-xs"
          >
            Add note
          </button>
        </form>
      </section>

      {/* Actions panel */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {!isResolved ? (
          <>
            {/* Assign */}
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
              <h3 className="text-xs uppercase tracking-wide text-zinc-500">
                Assign
              </h3>
              <form
                action={assignTicketAction}
                className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-2 items-end"
              >
                <input
                  type="hidden"
                  name="ticket_id"
                  value={ticket.ticket_id}
                />
                <label className="flex flex-col gap-1 text-xs">
                  Assigned to
                  <input
                    name="assigned_to"
                    defaultValue={ticket.assigned_to ?? ""}
                    placeholder="dr_harshit / nurse_priya / leave blank to unassign"
                    className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                  />
                </label>
                <button
                  type="submit"
                  className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-xs"
                >
                  Save assignment
                </button>
              </form>
            </div>

            {/* Snooze */}
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
              <h3 className="text-xs uppercase tracking-wide text-zinc-500">
                Snooze
              </h3>
              {ticket.is_snoozed ? (
                <form action={unsnoozeTicketAction} className="mt-2">
                  <input
                    type="hidden"
                    name="ticket_id"
                    value={ticket.ticket_id}
                  />
                  <p className="text-xs text-zinc-500 mb-2">
                    Currently snoozed until{" "}
                    {formatDateTime(ticket.snoozed_until ?? "")}.
                  </p>
                  <button
                    type="submit"
                    className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-xs"
                  >
                    Clear snooze
                  </button>
                </form>
              ) : (
                <form
                  action={snoozeTicketAction}
                  className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 items-end"
                >
                  <input
                    type="hidden"
                    name="ticket_id"
                    value={ticket.ticket_id}
                  />
                  <label className="flex flex-col gap-1 text-xs">
                    For (minutes)
                    <input
                      name="minutes"
                      type="number"
                      min={1}
                      max={10080}
                      defaultValue={60}
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs sm:col-span-1">
                    Notes (optional)
                    <input
                      name="notes"
                      placeholder="awaiting callback"
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                    />
                  </label>
                  <button
                    type="submit"
                    className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-xs"
                  >
                    Snooze
                  </button>
                </form>
              )}
            </div>

            {/* Acknowledge */}
            {isOpen ? (
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
                <h3 className="text-xs uppercase tracking-wide text-zinc-500">
                  Acknowledge
                </h3>
                <form
                  action={ackTicketFormAction}
                  className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-2 items-end"
                >
                  <input
                    type="hidden"
                    name="ticket_id"
                    value={ticket.ticket_id}
                  />
                  <label className="flex flex-col gap-1 text-xs">
                    Notes (optional)
                    <input
                      name="notes"
                      placeholder="working it now"
                      className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                    />
                  </label>
                  <button
                    type="submit"
                    className="px-3 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs"
                  >
                    Acknowledge
                  </button>
                </form>
              </div>
            ) : null}

            {/* Resolve */}
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
              <h3 className="text-xs uppercase tracking-wide text-zinc-500">
                Resolve
              </h3>
              <form
                action={resolveTicketFormAction}
                className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-2 items-end"
              >
                <input
                  type="hidden"
                  name="ticket_id"
                  value={ticket.ticket_id}
                />
                <label className="flex flex-col gap-1 text-xs">
                  Resolution notes
                  <input
                    name="notes"
                    placeholder="patient confirmed refill picked up"
                    className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                  />
                </label>
                <button
                  type="submit"
                  className="px-3 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs"
                >
                  Resolve
                </button>
              </form>
            </div>
          </>
        ) : (
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 lg:col-span-2">
            <h3 className="text-xs uppercase tracking-wide text-zinc-500">
              Reopen
            </h3>
            <p className="text-xs text-zinc-500 mt-1">
              Resolved at{" "}
              {ticket.resolved_at ? formatDateTime(ticket.resolved_at) : "—"}.
              Reopen if the issue recurred — SLA re-fires from now.
            </p>
            <form
              action={reopenTicketAction}
              className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-2 items-end"
            >
              <input
                type="hidden"
                name="ticket_id"
                value={ticket.ticket_id}
              />
              <label className="flex flex-col gap-1 text-xs">
                Reason
                <input
                  name="notes"
                  placeholder="patient called back — issue not resolved"
                  className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
                />
              </label>
              <button
                type="submit"
                className="px-3 py-2 rounded border border-amber-300 text-amber-700 hover:bg-amber-50 dark:border-amber-800 dark:text-amber-300 dark:hover:bg-amber-950/40 text-xs"
              >
                Reopen
              </button>
            </form>
          </div>
        )}
      </section>
    </div>
  );
}
