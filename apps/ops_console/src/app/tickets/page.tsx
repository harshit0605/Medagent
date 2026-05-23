import Link from "next/link";

import { orchestrator, type OpsTicket } from "@/lib/backend";

import { TicketActions } from "./_components/TicketActions";

export const dynamic = "force-dynamic";

const VIEW_FILTERS = ["active", "all", "snoozed", "resolved"] as const;
type ViewFilter = (typeof VIEW_FILTERS)[number];

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

function SlaChip({ ticket }: { ticket: OpsTicket }) {
  // A resolved ticket with a stamped breach still shows the breach
  // marker — historical signal so ops can see "this got handled but
  // we missed the SLA window". Without it, resolved breaches would
  // disappear from the UI even though they're the most actionable
  // pattern (which categories systematically miss SLA?).
  if (ticket.status === "resolved") {
    if (ticket.sla_breached_at) {
      return (
        <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200">
          breached SLA · {formatRelative(ticket.sla_breached_at)}
        </span>
      );
    }
    return <span className="text-[11px] text-zinc-400">—</span>;
  }
  if (ticket.is_snoozed && ticket.snoozed_until) {
    return (
      <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
        snoozed · resumes {formatRelative(ticket.snoozed_until)}
      </span>
    );
  }
  // Persistent first-cross marker takes precedence over the live
  // `is_overdue` chip — once the sweep has stamped the breach, the
  // UI shows when we noticed it (durable) instead of the moving
  // "was due Xh ago" target.
  if (ticket.sla_breached_at) {
    return (
      <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-200 text-red-900 dark:bg-red-900/60 dark:text-red-100 font-semibold">
        breached · stamped {formatRelative(ticket.sla_breached_at)}
      </span>
    );
  }
  if (ticket.is_overdue) {
    return (
      <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200 font-medium">
        overdue · was due {formatRelative(ticket.sla_due_at)}
      </span>
    );
  }
  return (
    <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
      due {formatRelative(ticket.sla_due_at)}
    </span>
  );
}

export default async function TicketsPage({
  searchParams,
}: {
  searchParams: Promise<{
    view?: string;
    category?: string;
    assigned_to?: string;
  }>;
}) {
  const params = await searchParams;
  const requested = (params.view ?? "active") as ViewFilter;
  const view: ViewFilter = VIEW_FILTERS.includes(requested) ? requested : "active";

  const queryParams: Parameters<typeof orchestrator.listTickets>[0] = {};
  if (view === "active") queryParams.view = "active";
  else if (view === "snoozed") queryParams.view = "snoozed";
  else if (view === "resolved") queryParams.status = "resolved";
  if (params.category) queryParams.category = params.category;
  if (params.assigned_to) queryParams.assigned_to = params.assigned_to;

  let tickets: OpsTicket[] = [];
  let error: string | null = null;
  try {
    tickets = await orchestrator.listTickets(queryParams);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  // Build category list from current ticket set for filter chips.
  const allCategories = Array.from(
    new Set(tickets.map((t) => t.category)),
  ).sort();
  const allAssignees = Array.from(
    new Set(tickets.map((t) => t.assigned_to).filter(Boolean) as string[]),
  ).sort();

  function urlFor(updates: Record<string, string | undefined>): string {
    const next = new URLSearchParams();
    if (view !== "active") next.set("view", view);
    if (params.category) next.set("category", params.category);
    if (params.assigned_to) next.set("assigned_to", params.assigned_to);
    for (const [k, v] of Object.entries(updates)) {
      if (v === undefined || v === "") next.delete(k);
      else next.set(k, v);
    }
    const qs = next.toString();
    return qs ? `/tickets?${qs}` : "/tickets";
  }

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <h1 className="text-xl font-semibold">Ops tickets</h1>
        <div className="flex gap-1 text-sm">
          {VIEW_FILTERS.map((v) => {
            const active = v === view;
            return (
              <Link
                key={v}
                href={urlFor({ view: v === "active" ? undefined : v })}
                className={
                  "px-3 py-1 rounded border " +
                  (active
                    ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                    : "border-zinc-200 dark:border-zinc-800 text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100")
                }
              >
                {v}
              </Link>
            );
          })}
        </div>
      </div>

      {/* Secondary filters: category + assignee. Hidden when there's nothing
          to filter on. */}
      {(allCategories.length > 1 || allAssignees.length > 0) ? (
        <div className="flex flex-wrap gap-2 text-xs">
          {allCategories.length > 1 ? (
            <>
              <span className="self-center text-zinc-500">category:</span>
              <Link
                href={urlFor({ category: undefined })}
                className={
                  "px-2 py-0.5 rounded border " +
                  (!params.category
                    ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                    : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
                }
              >
                all
              </Link>
              {allCategories.map((c) => (
                <Link
                  key={c}
                  href={urlFor({ category: c })}
                  className={
                    "px-2 py-0.5 rounded border " +
                    (params.category === c
                      ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                      : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
                  }
                >
                  {c}
                </Link>
              ))}
            </>
          ) : null}
          {allAssignees.length > 0 ? (
            <>
              <span className="self-center text-zinc-500 ml-2">assigned:</span>
              <Link
                href={urlFor({ assigned_to: undefined })}
                className={
                  "px-2 py-0.5 rounded border " +
                  (!params.assigned_to
                    ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                    : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
                }
              >
                anyone
              </Link>
              {allAssignees.map((a) => (
                <Link
                  key={a}
                  href={urlFor({ assigned_to: a })}
                  className={
                    "px-2 py-0.5 rounded border " +
                    (params.assigned_to === a
                      ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                      : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
                  }
                >
                  {a}
                </Link>
              ))}
            </>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t list tickets. <code className="font-mono text-xs">{error}</code>
        </div>
      ) : tickets.length === 0 ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          No tickets in this view.
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden bg-white dark:bg-zinc-900">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
              <tr>
                <th className="text-left px-4 py-2">ID</th>
                <th className="text-left px-4 py-2">Patient</th>
                <th className="text-left px-4 py-2">Category</th>
                <th className="text-left px-4 py-2">Pri</th>
                <th className="text-left px-4 py-2">SLA</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Assignee</th>
                <th className="text-left px-4 py-2">Created</th>
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
              {tickets.map((ticket) => (
                <tr
                  key={ticket.ticket_id}
                  className={
                    // Persistently-breached rows stay highlighted even
                    // after resolution so ops can scan for systemic
                    // SLA misses; live-overdue rows get the same
                    // treatment.
                    ticket.sla_breached_at || ticket.is_overdue
                      ? "bg-red-50/30 dark:bg-red-950/10"
                      : ""
                  }
                >
                  <td className="px-4 py-3 font-mono text-xs">
                    <Link
                      href={`/tickets/${ticket.ticket_id}`}
                      className="text-blue-600 hover:underline dark:text-blue-400"
                    >
                      #{ticket.ticket_id}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    {ticket.patient_db_id !== null ? (
                      <Link
                        href={`/patients/${ticket.patient_db_id}`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {ticket.patient_full_name ?? ticket.patient_id}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs">
                        {ticket.patient_id}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs">{ticket.category}</td>
                  <td className="px-4 py-3">
                    <span
                      className={
                        "inline-block px-2 py-0.5 rounded text-xs font-medium " +
                        (PRIORITY_BADGE[ticket.priority] ?? PRIORITY_BADGE.p3)
                      }
                    >
                      {ticket.priority}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <SlaChip ticket={ticket} />
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={
                        "inline-block px-2 py-0.5 rounded text-xs font-medium " +
                        (STATUS_BADGE[ticket.status] ?? "")
                      }
                    >
                      {ticket.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-zinc-500">
                    {ticket.assigned_to ?? (
                      <span className="text-zinc-400">unassigned</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-zinc-500">
                    {formatRelative(ticket.created_at)}
                  </td>
                  <td className="px-4 py-3">
                    <TicketActions
                      ticketId={ticket.ticket_id}
                      status={ticket.status}
                    />
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
