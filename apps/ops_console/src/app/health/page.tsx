import { orchestrator, type HealthSummary } from "@/lib/backend";

export const dynamic = "force-dynamic";

const COMPONENT_LABEL: Record<string, string> = {
  "scheduler.dispatch": "Dispatcher",
  "scheduler.dose_materialize": "Dose / refill / lab materializer",
  "scheduler.missed_dose_sweep": "Missed-dose sweep",
  "scheduler.recap_sweep": "Recap lifecycle sweep",
  "scheduler.care_gap_sweep": "Care-gap sweep",
};

function formatRelative(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

function statusTone(
  outcome: string,
  isStale: boolean,
  consecutiveErrors: number,
): "ok" | "warn" | "danger" {
  if (consecutiveErrors >= 3 || (isStale && outcome === "error")) {
    return "danger";
  }
  if (isStale || outcome === "error") return "warn";
  return "ok";
}

const TONE_BADGE: Record<string, string> = {
  ok: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  warn: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  danger:
    "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200 border border-red-300 dark:border-red-800",
};

const TONE_LABEL: Record<string, string> = {
  ok: "OK",
  warn: "Warning",
  danger: "Critical",
};

function MetricCard({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "warn" | "danger";
}) {
  const valueClass =
    tone === "danger"
      ? "text-red-600 dark:text-red-400"
      : tone === "warn"
        ? "text-amber-600 dark:text-amber-400"
        : "";
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
      <div className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
        {label}
      </div>
      <div className={`mt-2 text-3xl font-semibold tabular-nums ${valueClass}`}>
        {value}
      </div>
      {hint ? <div className="mt-1 text-sm text-zinc-500">{hint}</div> : null}
    </div>
  );
}

export default async function HealthPage() {
  let health: HealthSummary | null = null;
  let error: string | null = null;
  try {
    health = await orchestrator.health();
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !health) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-semibold">Service health</h1>
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t reach <code>/ops/health</code>.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      </div>
    );
  }

  const overallTone =
    health.stuck_components > 0 ||
    health.failed_events_24h > 5 ||
    health.error_components > 0
      ? "danger"
      : health.pending_overdue > 0
        ? "warn"
        : "ok";

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <h1 className="text-xl font-semibold">Service health</h1>
        <span
          className={
            "px-3 py-1 rounded-md text-sm font-medium " +
            TONE_BADGE[overallTone]
          }
        >
          Overall: {TONE_LABEL[overallTone]}
        </span>
      </div>

      <p className="text-sm text-zinc-500 max-w-2xl">
        Each scheduler loop writes a heartbeat at the end of every pass.
        A row goes <strong>warn</strong> if its last run is older than the
        loop&apos;s expected cadence, <strong>critical</strong> if it has
        consecutive errors. Failed-events + stuck-pending counts come
        from <code>scheduled_events</code>.
      </p>

      <section className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <MetricCard
          label="Failed events (24h)"
          value={health.failed_events_24h}
          hint="ScheduledEvent.status = failed"
          tone={health.failed_events_24h > 5 ? "danger" : undefined}
        />
        <MetricCard
          label="Pending overdue"
          value={health.pending_overdue}
          hint="scheduled_for + 1h has elapsed"
          tone={health.pending_overdue > 0 ? "warn" : undefined}
        />
        <MetricCard
          label="Stuck components"
          value={health.stuck_components}
          hint="No heartbeat in cadence"
          tone={health.stuck_components > 0 ? "danger" : undefined}
        />
        <MetricCard
          label="Errored components"
          value={health.error_components}
          hint="Last pass = error"
          tone={health.error_components > 0 ? "danger" : undefined}
        />
      </section>

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Components
        </h2>
        <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden bg-white dark:bg-zinc-900">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
              <tr>
                <th className="text-left px-4 py-2">Component</th>
                <th className="text-left px-4 py-2 w-24">Status</th>
                <th className="text-left px-4 py-2 w-40">Last run</th>
                <th className="text-left px-4 py-2 w-28">Consec errors</th>
                <th className="text-left px-4 py-2">Last details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
              {health.components.length === 0 ? (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-6 text-center text-sm text-zinc-500"
                  >
                    No heartbeats recorded yet — start the scheduler and
                    let one loop tick through.
                  </td>
                </tr>
              ) : (
                health.components.map((c) => {
                  const tone = statusTone(
                    c.last_outcome,
                    c.is_stale,
                    c.consecutive_errors,
                  );
                  return (
                    <tr key={c.component}>
                      <td className="px-4 py-3">
                        <div className="font-medium">
                          {COMPONENT_LABEL[c.component] ?? c.component}
                        </div>
                        <div className="text-[11px] text-zinc-500 font-mono">
                          {c.component}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={
                            "inline-block px-2 py-0.5 rounded text-xs font-medium " +
                            TONE_BADGE[tone]
                          }
                        >
                          {TONE_LABEL[tone]}
                        </span>
                        <div className="mt-1 text-[11px] text-zinc-500">
                          last: {c.last_outcome}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-xs">
                        <div>{formatRelative(c.seconds_since_last_run)}</div>
                        <div className="text-zinc-500">
                          {formatDateTime(c.last_run_at)}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-sm tabular-nums">
                        {c.consecutive_errors > 0 ? (
                          <span className="text-red-600 dark:text-red-400 font-medium">
                            {c.consecutive_errors}
                          </span>
                        ) : (
                          <span className="text-zinc-400">0</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <pre className="text-[11px] text-zinc-500 font-mono whitespace-pre-wrap break-all max-w-md">
                          {Object.keys(c.details ?? {}).length === 0
                            ? "—"
                            : JSON.stringify(c.details, null, 2).slice(
                                0,
                                300,
                              )}
                        </pre>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
