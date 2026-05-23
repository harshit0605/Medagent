import Link from "next/link";

import { orchestrator, type PreVisitSummary } from "@/lib/backend";

export const dynamic = "force-dynamic";

const COHORT_LABEL: Record<string, string> = {
  cohort_diabetes: "Diabetes",
  cohort_cardiac: "Cardiac",
  cohort_fall_risk: "Fall risk",
};

const URGENCY_BADGE: Record<string, string> = {
  critical:
    "bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-200 border border-red-300 dark:border-red-800",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200",
  medium:
    "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-200",
  low: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
};

const PRIORITY_BADGE: Record<string, string> = {
  p0: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  p1: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200",
  p2: "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-200",
  p3: "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
};

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const diffSec = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(diffSec);
  if (abs < 60) return `${abs}s ago`;
  if (abs < 3600) return `${Math.round(abs / 60)}m ago`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h ago`;
  return `${Math.round(abs / 86400)}d ago`;
}

function regimenScheduleSummary(r: PreVisitSummary["regimens"][number]): string {
  const times = (r.schedule?.times as string[] | undefined) ?? [];
  const tz = (r.schedule?.timezone as string | undefined) ?? "UTC";
  if (times.length === 0) return "(no schedule)";
  return `${times.join(", ")} · ${tz}`;
}

export default async function AppointmentPreVisitPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const appointmentId = Number(id);
  if (!Number.isFinite(appointmentId) || appointmentId <= 0) {
    return <div className="text-sm text-red-600">Invalid appointment id.</div>;
  }

  let summary: PreVisitSummary | null = null;
  let error: string | null = null;
  try {
    summary = await orchestrator.getPreVisitSummary(appointmentId);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !summary) {
    return (
      <div className="space-y-3">
        <Link
          href="/patients"
          className="text-sm text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          ← back
        </Link>
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load pre-visit summary.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      </div>
    );
  }

  const a = summary.appointment;
  const p = summary.patient;
  const adh = summary.adherence;

  // Order recent inbox by urgency desc (critical first), then time desc.
  const urgencyOrder: Record<string, number> = {
    critical: 0,
    high: 1,
    medium: 2,
    low: 3,
  };
  const orderedInbox = [...summary.recent_inbox].sort((x, y) => {
    const ux = urgencyOrder[x.urgency] ?? 4;
    const uy = urgencyOrder[y.urgency] ?? 4;
    if (ux !== uy) return ux - uy;
    return new Date(y.created_at).getTime() - new Date(x.created_at).getTime();
  });

  return (
    <div className="space-y-6 max-w-5xl">
      {/* Header */}
      <div>
        <Link
          href={`/patients/${a.patient_id}`}
          className="text-sm text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          ← back to patient
        </Link>
        <div className="mt-2 flex items-baseline justify-between flex-wrap gap-3">
          <div>
            <h1 className="text-xl font-semibold">
              Pre-visit brief
            </h1>
            <div className="mt-1 text-sm text-zinc-500">
              {p.full_name}
              {" · "}
              {a.doctor_name ?? `Doctor #${a.doctor_id}`}
              {" · "}
              {formatDateTime(a.scheduled_for)}
              {" · status: "}
              <span className="font-medium">{a.status}</span>
            </div>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <Link
              href={`/appointments/${a.id}/recap`}
              className="px-3 py-1.5 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white"
            >
              Compose / view recap →
            </Link>
            {a.calendar_html_link ? (
              <a
                href={a.calendar_html_link}
                target="_blank"
                rel="noopener noreferrer"
                className="px-3 py-1.5 rounded-md border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-50 dark:hover:bg-zinc-800"
              >
                Calendar event ↗
              </a>
            ) : null}
          </div>
        </div>

        {/* Cohort badges */}
        <div className="mt-3 flex flex-wrap gap-2">
          {summary.cohort_flags.map((c) => (
            <span
              key={c}
              className="inline-block px-2 py-0.5 rounded text-xs bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200"
              title="Built-in cohort flag"
            >
              {COHORT_LABEL[c] ?? c}
            </span>
          ))}
          {summary.cohort_tags.map((t) => (
            <span
              key={t.cohort_tag_id}
              className="inline-block px-2 py-0.5 rounded text-xs bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-200"
              title="Cohort tag"
            >
              {t.label}
            </span>
          ))}
          {summary.has_caregiver_cc ? (
            <span
              className="inline-block px-2 py-0.5 rounded text-xs bg-purple-100 text-purple-800 dark:bg-purple-950 dark:text-purple-200"
              title="Active caregivers receive recap copies"
            >
              Caregiver cc on
            </span>
          ) : null}
        </div>
      </div>

      {/* Adherence + key metrics */}
      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-[11px] uppercase tracking-wide text-zinc-500">
            Adherence (30d)
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {pct(adh.adherence_rate)}
          </div>
          <div className="text-[11px] text-zinc-500">
            {adh.taken}/{adh.total} doses
          </div>
        </div>
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-[11px] uppercase tracking-wide text-zinc-500">
            On-time
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {pct(adh.on_time_rate)}
          </div>
          <div className="text-[11px] text-zinc-500">
            {adh.taken_late} late · {adh.missed} missed
          </div>
        </div>
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-[11px] uppercase tracking-wide text-zinc-500">
            Active regimens
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {summary.regimens.length}
          </div>
        </div>
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-[11px] uppercase tracking-wide text-zinc-500">
            Open ops items
          </div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {summary.open_tickets.length}
          </div>
          <div className="text-[11px] text-zinc-500">
            {summary.open_tickets.filter((t) => t.is_overdue).length} overdue
          </div>
        </div>
      </section>

      {/* What to focus on — only renders if anything actually warrants it */}
      {(orderedInbox.some((i) => i.urgency === "critical" || i.urgency === "high") ||
        summary.open_tickets.some((t) => t.is_overdue) ||
        summary.open_lab_followups.some((l) => l.is_overdue)) ? (
        <section className="rounded-lg border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-4 space-y-2">
          <h2 className="text-sm font-semibold text-amber-900 dark:text-amber-200 uppercase tracking-wide">
            What to focus on
          </h2>
          <ul className="text-sm text-amber-900 dark:text-amber-100 space-y-1 list-disc pl-5">
            {orderedInbox
              .filter(
                (i) => i.urgency === "critical" || i.urgency === "high",
              )
              .slice(0, 5)
              .map((i) => (
                <li key={`focus-inbox-${i.id}`}>
                  <span className="font-medium">[{i.urgency}]</span>{" "}
                  {i.summary ?? i.inbound_text ?? "(message)"}{" "}
                  <span className="text-xs text-amber-700 dark:text-amber-300">
                    ({formatRelative(i.created_at)})
                  </span>
                </li>
              ))}
            {summary.open_lab_followups
              .filter((l) => l.is_overdue)
              .map((l) => (
                <li key={`focus-lab-${l.id}`}>
                  Overdue lab: <span className="font-medium">{l.test_name}</span>{" "}
                  <span className="text-xs">
                    (was due{" "}
                    {l.due_by
                      ? new Date(l.due_by).toLocaleDateString()
                      : "—"}
                    )
                  </span>
                </li>
              ))}
            {summary.open_tickets
              .filter((t) => t.is_overdue)
              .map((t) => (
                <li key={`focus-ticket-${t.ticket_id}`}>
                  Overdue ticket{" "}
                  <Link
                    href={`/tickets/${t.ticket_id}`}
                    className="underline"
                  >
                    #{t.ticket_id}
                  </Link>{" "}
                  ({t.category}, {t.priority})
                </li>
              ))}
          </ul>
        </section>
      ) : null}

      {/* Last visit recap excerpt */}
      {summary.last_recap ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Prior visit recap{" "}
            <span className="text-zinc-400 normal-case">
              · {formatDateTime(summary.last_recap.appointment_date)} ·{" "}
              {summary.last_recap.status}
            </span>
          </h2>
          <pre className="mt-2 whitespace-pre-wrap rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-4 text-sm font-sans text-zinc-800 dark:text-zinc-200">
            {summary.last_recap.summary}
          </pre>
          <Link
            href={`/appointments/${summary.last_recap.appointment_id}/recap`}
            className="mt-1 inline-block text-xs text-blue-600 hover:underline dark:text-blue-400"
          >
            full recap →
          </Link>
        </section>
      ) : null}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Regimens */}
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Active regimens
          </h2>
          <div className="mt-2 space-y-2">
            {summary.regimens.length === 0 ? (
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-3 text-xs text-zinc-500 text-center">
                None.
              </div>
            ) : (
              summary.regimens.map((r) => (
                <div
                  key={r.id}
                  className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3"
                >
                  <div className="flex items-baseline justify-between gap-3 flex-wrap">
                    <div className="font-medium">
                      {r.medication_name}{" "}
                      <span className="text-xs text-zinc-500">{r.dose}</span>
                    </div>
                    {r.days_of_supply_remaining !== null &&
                    r.days_of_supply_remaining !== undefined ? (
                      <span
                        className={
                          "text-xs " +
                          (r.days_of_supply_remaining <= 7
                            ? "text-amber-700 dark:text-amber-300 font-medium"
                            : "text-zinc-500")
                        }
                      >
                        {r.days_of_supply_remaining}d supply
                      </span>
                    ) : null}
                  </div>
                  <div className="text-xs text-zinc-500 mt-1">
                    {regimenScheduleSummary(r)}
                  </div>
                </div>
              ))
            )}
          </div>
        </section>

        {/* Open lab follow-ups */}
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Open lab follow-ups
          </h2>
          <div className="mt-2 space-y-2">
            {summary.open_lab_followups.length === 0 ? (
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-3 text-xs text-zinc-500 text-center">
                None.
              </div>
            ) : (
              summary.open_lab_followups.map((l) => (
                <div
                  key={l.id}
                  className={
                    "rounded-lg border p-3 " +
                    (l.is_overdue
                      ? "border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30"
                      : "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900")
                  }
                >
                  <div className="flex items-baseline justify-between gap-3 flex-wrap">
                    <div className="font-medium">{l.test_name}</div>
                    <span className="text-xs text-zinc-500">
                      {l.status}
                      {l.due_by ? ` · due ${new Date(l.due_by).toLocaleDateString()}` : ""}
                    </span>
                  </div>
                </div>
              ))
            )}
          </div>
        </section>
      </div>

      {/* Recent inbox */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Recent patient messages
        </h2>
        <div className="mt-2 space-y-2">
          {orderedInbox.length === 0 ? (
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-3 text-xs text-zinc-500 text-center">
              No recent inbound.
            </div>
          ) : (
            orderedInbox.slice(0, 8).map((i) => (
              <div
                key={i.id}
                className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3"
              >
                <div className="flex items-baseline justify-between gap-3 flex-wrap">
                  <div className="flex items-center gap-2">
                    <span
                      className={
                        "inline-block px-2 py-0.5 rounded text-[10px] font-medium uppercase " +
                        (URGENCY_BADGE[i.urgency] ?? URGENCY_BADGE.low)
                      }
                    >
                      {i.urgency}
                    </span>
                    <span className="text-xs text-zinc-500">
                      {i.category}
                      {i.handler_used ? ` · ${i.handler_used}` : ""}
                      {i.escalated ? " · escalated" : ""}
                    </span>
                  </div>
                  <span className="text-[11px] text-zinc-500">
                    {formatRelative(i.created_at)}
                  </span>
                </div>
                {i.summary ? (
                  <div className="mt-1 text-sm text-zinc-800 dark:text-zinc-200">
                    {i.summary}
                  </div>
                ) : null}
                {i.inbound_text ? (
                  <div className="mt-1 text-xs text-zinc-500 italic">
                    &ldquo;{i.inbound_text.slice(0, 200)}
                    {i.inbound_text.length > 200 ? "…" : ""}&rdquo;
                  </div>
                ) : null}
              </div>
            ))
          )}
        </div>
      </section>

      {/* Open ops tickets + active exemptions side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Open ops tickets
          </h2>
          <div className="mt-2 space-y-2">
            {summary.open_tickets.length === 0 ? (
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-3 text-xs text-zinc-500 text-center">
                None.
              </div>
            ) : (
              summary.open_tickets.map((t) => (
                <Link
                  key={t.ticket_id}
                  href={`/tickets/${t.ticket_id}`}
                  className={
                    "block rounded-lg border p-3 hover:bg-zinc-50 dark:hover:bg-zinc-800 " +
                    (t.is_overdue
                      ? "border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30"
                      : "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900")
                  }
                >
                  <div className="flex items-baseline justify-between gap-3 flex-wrap">
                    <div className="text-sm">
                      <span className="font-mono text-xs text-zinc-500">
                        #{t.ticket_id}
                      </span>{" "}
                      <span className="font-medium">{t.category}</span>
                    </div>
                    <div className="flex gap-2 items-center text-[11px]">
                      <span
                        className={
                          "inline-block px-2 py-0.5 rounded font-medium " +
                          (PRIORITY_BADGE[t.priority] ?? PRIORITY_BADGE.p3)
                        }
                      >
                        {t.priority}
                      </span>
                      <span className="text-zinc-500">
                        {t.status}
                        {t.is_overdue ? " · overdue" : ""}
                        {t.is_snoozed ? " · snoozed" : ""}
                      </span>
                    </div>
                  </div>
                </Link>
              ))
            )}
          </div>
        </section>

        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Active exemptions
          </h2>
          <div className="mt-2 space-y-2">
            {summary.active_exemptions.length === 0 ? (
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-3 text-xs text-zinc-500 text-center">
                None.
              </div>
            ) : (
              summary.active_exemptions.map((ex) => (
                <div
                  key={ex.id}
                  className="rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-3"
                >
                  <div className="font-medium text-sm">
                    {ex.care_plan_test_name ?? `Plan #${ex.care_plan_id}`}
                  </div>
                  <div className="mt-1 text-xs text-zinc-700 dark:text-zinc-300">
                    {ex.reason}
                  </div>
                  {ex.expires_at ? (
                    <div className="mt-1 text-[11px] text-zinc-500">
                      Expires {formatDateTime(ex.expires_at)}
                    </div>
                  ) : null}
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
