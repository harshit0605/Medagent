import Link from "next/link";

import { orchestrator } from "@/lib/backend";

export const dynamic = "force-dynamic";

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function MetricCard({
  label,
  value,
  hint,
  tone,
  href,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "warn" | "danger";
  href?: string;
}) {
  const valueClass =
    tone === "danger"
      ? "text-red-600 dark:text-red-400"
      : tone === "warn"
        ? "text-amber-600 dark:text-amber-400"
        : "";
  const card = (
    <div
      className={
        "rounded-lg border bg-white dark:bg-zinc-900 p-5 " +
        (tone
          ? "border-zinc-200 dark:border-zinc-800 hover:border-zinc-300 dark:hover:border-zinc-700"
          : "border-zinc-200 dark:border-zinc-800")
      }
    >
      <div className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
        {label}
      </div>
      <div className={`mt-2 text-3xl font-semibold tabular-nums ${valueClass}`}>
        {value}
      </div>
      {hint ? <div className="mt-1 text-sm text-zinc-500">{hint}</div> : null}
    </div>
  );
  return href ? (
    <Link href={href} className="block">
      {card}
    </Link>
  ) : (
    card
  );
}

export default async function DashboardPage() {
  let dashboard;
  let error: string | null = null;
  try {
    dashboard = await orchestrator.dashboard();
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !dashboard) {
    return (
      <div>
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <div className="mt-4 rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t reach the orchestrator at <code>/ops/dashboard</code>.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      </div>
    );
  }

  const { program_metrics, queue, alerts, delivery, delivery_by_template } =
    dashboard;
  const a = alerts ?? {
    regimens_running_low: 0,
    missed_dose_escalations_open: 0,
    refill_help_open: 0,
    labs_overdue: 0,
    lab_help_open: 0,
    prescriptions_pending: 0,
    tickets_sla_overdue: 0,
    care_gaps_open: 0,
  };
  // Delivery is OPTIONAL on the wire so older dashboards still render
  // before the column is populated. Default to a zero-state shape.
  const d = delivery ?? {
    since: "",
    total_outbound: 0,
    by_status: {
      delivered: 0,
      read: 0,
      sent_only: 0,
      failed: 0,
      failed_pre_meta: 0,
      no_status_yet: 0,
    },
    delivery_rate: 0,
    failure_rate: 0,
    by_payload_kind: {},
    top_failure_codes: [],
  };
  // Tile tone: any failure raises a "warn" tint; >5% failure rate or
  // any pre-meta failures bumps to "danger". Lets ops scan and only
  // dig in when something's actually red.
  const failedAny = d.by_status.failed + d.by_status.failed_pre_meta;
  const deliveryTone =
    d.by_status.failed_pre_meta > 0 || d.failure_rate > 0.05
      ? "danger"
      : failedAny > 0
        ? "warn"
        : undefined;

  return (
    <div className="space-y-8">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <Link
          href="/tickets"
          className="text-sm text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          View tickets →
        </Link>
      </div>

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Program metrics
        </h2>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-4">
          <MetricCard
            label="Adherence rate"
            value={pct(program_metrics.adherence_rate)}
            hint="Doses taken / total scheduled"
          />
          <MetricCard
            label="Refill risk"
            value={pct(program_metrics.refill_risk_rate)}
            hint="Recoveries needing refill support"
          />
          <MetricCard
            label="Follow-up closure"
            value={pct(program_metrics.followup_closure_rate)}
            hint="Lab + appointment items reviewed"
          />
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Care alerts
        </h2>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 lg:grid-cols-4 gap-4">
          <MetricCard
            label="Tickets SLA overdue"
            value={String(a.tickets_sla_overdue)}
            hint="Past due, not snoozed"
            tone={a.tickets_sla_overdue > 0 ? "danger" : undefined}
            href="/tickets?view=active"
          />
          <MetricCard
            label="Care gaps open"
            value={String(a.care_gaps_open)}
            hint="Cohort tests overdue"
            tone={a.care_gaps_open > 0 ? "warn" : undefined}
            href="/patients"
          />
          <MetricCard
            label="Regimens running low"
            value={String(a.regimens_running_low)}
            hint="≤7 days of supply"
            tone={a.regimens_running_low > 0 ? "warn" : undefined}
            href="/patients"
          />
          <MetricCard
            label="Missed-dose escalations"
            value={String(a.missed_dose_escalations_open)}
            hint="Consecutive missed doses"
            tone={
              a.missed_dose_escalations_open > 0 ? "danger" : undefined
            }
            href="/tickets"
          />
          <MetricCard
            label="Refill help open"
            value={String(a.refill_help_open)}
            hint="Need help / snooze cap"
            tone={a.refill_help_open > 0 ? "warn" : undefined}
            href="/tickets"
          />
          <MetricCard
            label="Labs overdue"
            value={String(a.labs_overdue)}
            hint="Due/booked past due_by"
            tone={a.labs_overdue > 0 ? "danger" : undefined}
            href="/patients"
          />
          <MetricCard
            label="Lab help open"
            value={String(a.lab_help_open)}
            hint="Patient asked for help"
            tone={a.lab_help_open > 0 ? "warn" : undefined}
            href="/tickets"
          />
          <MetricCard
            label="Prescriptions pending"
            value={String(a.prescriptions_pending)}
            hint="Awaiting clinician review"
            tone={a.prescriptions_pending > 0 ? "warn" : undefined}
            href="/prescriptions"
          />
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Ops queue
        </h2>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard label="Open" value={String(queue.open)} />
          <MetricCard label="Acknowledged" value={String(queue.acknowledged)} />
          <MetricCard label="Resolved" value={String(queue.resolved)} />
          <MetricCard label="Total" value={String(queue.total)} />
        </div>
      </section>

      <section>
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            WhatsApp delivery (last 24h)
          </h2>
          {d.total_outbound > 0 ? (
            <span className="text-xs text-zinc-400">
              {d.total_outbound.toLocaleString()} outbound · joined to status webhooks
            </span>
          ) : null}
        </div>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard
            label="Delivery rate"
            value={pct(d.delivery_rate)}
            hint={`${d.by_status.delivered + d.by_status.read} of ${d.total_outbound}`}
            tone={deliveryTone}
          />
          <MetricCard
            label="Failure rate"
            value={pct(d.failure_rate)}
            hint={
              d.by_status.failed_pre_meta > 0
                ? `${d.by_status.failed_pre_meta} pre-Meta · ${d.by_status.failed} Meta-side`
                : `${d.by_status.failed} Meta-side failures`
            }
            tone={
              d.failure_rate > 0.05 || d.by_status.failed_pre_meta > 0
                ? "danger"
                : d.by_status.failed > 0
                  ? "warn"
                  : undefined
            }
          />
          <MetricCard
            label="In flight"
            value={String(d.by_status.sent_only + d.by_status.no_status_yet)}
            hint={
              d.by_status.no_status_yet > 0
                ? `${d.by_status.sent_only} sent · ${d.by_status.no_status_yet} awaiting webhook`
                : `${d.by_status.sent_only} awaiting delivery`
            }
          />
          <MetricCard
            label="Read"
            value={String(d.by_status.read)}
            hint="Recipient opened"
          />
        </div>
        {/* Per-payload-kind breakdown — small text, only render
            when there's actually data to show. Keeps the dashboard
            compact while still surfacing template-vs-freeform splits
            when ops are debugging a specific failure pattern. */}
        {d.total_outbound > 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 text-xs text-zinc-600 dark:text-zinc-400">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1">
              {Object.entries(d.by_payload_kind)
                .filter(([, v]) => v.total > 0)
                .map(([kind, v]) => {
                  const rate = v.total > 0 ? v.delivered / v.total : 0;
                  return (
                    <div key={kind}>
                      <span className="font-medium capitalize">{kind}</span>{" "}
                      <span className="tabular-nums">
                        {pct(rate)} · {v.delivered}/{v.total}
                      </span>
                      {v.failed > 0 ? (
                        <span className="text-red-600 dark:text-red-400 ml-1">
                          ({v.failed} failed)
                        </span>
                      ) : null}
                    </div>
                  );
                })}
            </div>
            {d.top_failure_codes.length > 0 ? (
              <div className="mt-2 pt-2 border-t border-zinc-200 dark:border-zinc-800">
                <span className="text-zinc-500">Top failure codes:</span>{" "}
                {d.top_failure_codes.map((e, i) => (
                  <span key={e.code} className="ml-1">
                    <code className="text-red-600 dark:text-red-400">
                      {e.code}
                    </code>{" "}
                    {e.title ?? "—"} ({e.count})
                    {i < d.top_failure_codes.length - 1 ? "," : ""}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Per-template delivery breakdown. The aggregate delivery
            tile blends every template — a single template (e.g. a
            new v2 template silently rejected by Meta) at 0% delivery
            while the rest stay healthy is invisible there. This
            section names names. Only renders when there's at least
            one template send in the window. */}
        {delivery_by_template && delivery_by_template.length > 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
            <div className="px-3 py-2 text-xs font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide border-b border-zinc-200 dark:border-zinc-800">
              By template
            </div>
            <table className="w-full text-xs">
              <thead className="bg-zinc-50 dark:bg-zinc-950 text-zinc-500 dark:text-zinc-400">
                <tr>
                  <th className="text-left px-3 py-1.5">Template</th>
                  <th className="text-right px-3 py-1.5">Sent</th>
                  <th className="text-right px-3 py-1.5">Delivered</th>
                  <th className="text-right px-3 py-1.5">Failed</th>
                  <th className="text-right px-3 py-1.5">Rate</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {delivery_by_template.map((row) => {
                  // Highlight any template with >5% failure rate, OR
                  // any pre-Meta failures (our-pipeline problem),
                  // OR a template seeing zero deliveries despite
                  // having sends (silent failure mode).
                  const danger =
                    row.failure_rate > 0.05 ||
                    row.failed_pre_meta > 0 ||
                    (row.total > 0 && row.delivered === 0);
                  const warn = !danger && row.failed > 0;
                  return (
                    <tr
                      key={row.template_name}
                      className={
                        danger
                          ? "bg-red-50/50 dark:bg-red-950/30"
                          : warn
                            ? "bg-amber-50/50 dark:bg-amber-950/30"
                            : ""
                      }
                    >
                      <td className="px-3 py-1.5 font-mono text-[11px]">
                        {row.template_name}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {row.total}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {row.delivered}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {row.failed + row.failed_pre_meta > 0 ? (
                          <span
                            className={
                              danger
                                ? "text-red-700 dark:text-red-300 font-medium"
                                : "text-amber-700 dark:text-amber-300"
                            }
                          >
                            {row.failed + row.failed_pre_meta}
                            {row.failed_pre_meta > 0 ? (
                              <span className="text-[10px] ml-1">
                                ({row.failed_pre_meta} pre-Meta)
                              </span>
                            ) : null}
                          </span>
                        ) : (
                          <span className="text-zinc-400">0</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {pct(row.delivery_rate)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
