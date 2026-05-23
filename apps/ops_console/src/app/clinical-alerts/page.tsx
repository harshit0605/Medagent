import Link from "next/link";

import {
  orchestrator,
  type ClinicalAlert,
  type ClinicalAlertSeverity,
  type ClinicalAlertStatus,
} from "@/lib/backend";

import {
  acknowledgeClinicalAlertAction,
  pageClinicalAlertAction,
  resolveClinicalAlertAction,
} from "./_actions";

// Same constant as backend's pager service so the UI's
// "max attempts reached" badge matches reality.
const MAX_PAGE_ATTEMPTS = 3;

export const dynamic = "force-dynamic";

// Severity ordering used to sort the open queue.
const SEVERITY_RANK: Record<ClinicalAlertSeverity, number> = {
  critical: 0,
  high: 1,
};

const SEVERITY_TONE: Record<ClinicalAlertSeverity, string> = {
  critical:
    "bg-red-100 text-red-900 border-red-300 dark:bg-red-950/60 dark:text-red-100 dark:border-red-800",
  high: "bg-amber-100 text-amber-900 border-amber-300 dark:bg-amber-950/50 dark:text-amber-100 dark:border-amber-800",
};

const STATUS_TONE: Record<ClinicalAlertStatus, string> = {
  open: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
  acknowledged:
    "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  resolved:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
};

function formatRelative(iso: string): string {
  const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

type Search = { status?: string };

export default async function ClinicalAlertsPage({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const params = await searchParams;
  const statusFilter: ClinicalAlertStatus | undefined =
    params.status === "open" ||
    params.status === "acknowledged" ||
    params.status === "resolved"
      ? params.status
      : undefined;

  let alerts: ClinicalAlert[] = [];
  let counts: Record<string, number> = {
    open: 0,
    acknowledged: 0,
    resolved: 0,
  };
  let error: string | null = null;
  try {
    [alerts, counts] = await Promise.all([
      orchestrator.listClinicalAlerts({
        status: statusFilter,
        limit: 100,
      }),
      orchestrator.clinicalAlertCounts(),
    ]);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  // Sort: critical-first, then by created_at descending. The
  // backend list_recent already returns newest-first; this
  // promotes critical above high within that ordering.
  const sorted = [...alerts].sort((a, b) => {
    const sa = SEVERITY_RANK[a.severity] ?? 99;
    const sb = SEVERITY_RANK[b.severity] ?? 99;
    if (sa !== sb) return sa - sb;
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Clinical alerts</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Patient-safety signals raised by the inbound triage classifier.
          Critical-first; ack to claim, resolve when reviewed. The bot has
          already replied to the patient — this queue is the doctor&apos;s
          parallel review path.
        </p>
      </div>

      {/* Status tiles. Each links to the filtered view. The
          critical-count subset of "open" is shown inline as a
          tooltip-style hint when relevant. */}
      <section className="grid grid-cols-3 gap-3">
        <FilterTile
          label="Open"
          value={counts.open}
          status={undefined}
          tone="red"
        />
        <FilterTile
          label="Acknowledged"
          value={counts.acknowledged}
          status="acknowledged"
          tone="amber"
        />
        <FilterTile
          label="Resolved"
          value={counts.resolved}
          status="resolved"
          tone="emerald"
        />
      </section>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-3 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load alerts.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : null}

      <section>
        <div className="flex items-baseline justify-between flex-wrap gap-2">
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            {statusFilter
              ? `${statusFilter} alerts`
              : "All recent alerts"}
          </h2>
          {statusFilter ? (
            <Link
              href="/clinical-alerts"
              className="text-xs text-blue-600 hover:underline dark:text-blue-400"
            >
              clear filter
            </Link>
          ) : (
            <Link
              href="/clinical-alerts?status=open"
              className="text-xs text-blue-600 hover:underline dark:text-blue-400"
            >
              show only open →
            </Link>
          )}
        </div>

        {sorted.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
            {statusFilter
              ? `No ${statusFilter} alerts.`
              : "No alerts yet — the queue is empty."}
          </div>
        ) : (
          <ol className="mt-3 space-y-3">
            {sorted.map((a) => (
              <AlertCard key={a.id} alert={a} />
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

function FilterTile({
  label,
  value,
  status,
  tone,
}: {
  label: string;
  value: number;
  status: ClinicalAlertStatus | undefined;
  tone: "red" | "amber" | "emerald";
}) {
  const valueClass =
    tone === "red" && value > 0
      ? "text-red-700 dark:text-red-300"
      : tone === "amber" && value > 0
        ? "text-amber-700 dark:text-amber-300"
        : tone === "emerald"
          ? "text-emerald-700 dark:text-emerald-300"
          : "";
  // "Open" is the default landing tile; clicking it filters
  // to status=open. Other tiles filter accordingly.
  const target = status
    ? `/clinical-alerts?status=${status}`
    : "/clinical-alerts?status=open";
  return (
    <Link
      href={target}
      className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 hover:border-zinc-400 dark:hover:border-zinc-600 transition-colors"
    >
      <div className={`text-2xl font-semibold tabular-nums ${valueClass}`}>
        {value}
      </div>
      <div className="text-xs text-zinc-500 mt-1 uppercase tracking-wide">
        {label}
      </div>
    </Link>
  );
}

function AlertCard({ alert }: { alert: ClinicalAlert }) {
  const tone = SEVERITY_TONE[alert.severity];
  const statusTone = STATUS_TONE[alert.status];
  return (
    <li className={`rounded-lg border-2 p-4 ${tone}`}>
      <header className="flex items-baseline justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold uppercase tracking-wide text-xs">
            {alert.severity === "critical" ? "🚨 critical" : "⚠️ high"}
          </span>
          <span
            className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${statusTone}`}
          >
            {alert.status}
          </span>
        </div>
        <span
          className="text-xs text-zinc-600 dark:text-zinc-400 tabular-nums"
          title={formatDateTime(alert.created_at)}
        >
          {formatRelative(alert.created_at)}
        </span>
      </header>

      <div className="mt-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">
        {alert.clinical_summary}
      </div>

      {alert.red_flags.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {alert.red_flags.map((rf, idx) => (
            <li
              key={idx}
              className="text-[10px] uppercase tracking-wide font-medium px-1.5 py-0.5 rounded bg-white/60 dark:bg-zinc-900/60 border border-current"
            >
              {rf}
            </li>
          ))}
        </ul>
      ) : null}

      <blockquote className="mt-3 pl-3 border-l-2 border-current text-sm italic whitespace-pre-line">
        {alert.inbound_text}
      </blockquote>

      {alert.severity === "critical" ? (
        <PagingStatus alert={alert} />
      ) : null}

      <footer className="mt-3 flex items-baseline justify-between flex-wrap gap-3">
        <div className="text-xs text-zinc-600 dark:text-zinc-400">
          <Link
            href={`/patients/${alert.patient_id}`}
            className="hover:underline"
          >
            patient #{alert.patient_id}
          </Link>
          {alert.acknowledged_by ? (
            <>
              {" "}
              · ack&apos;d by{" "}
              <span className="font-mono">{alert.acknowledged_by}</span>
            </>
          ) : null}
          {alert.resolved_by ? (
            <>
              {" "}
              · resolved by{" "}
              <span className="font-mono">{alert.resolved_by}</span>
            </>
          ) : null}
        </div>
        <div className="flex gap-2">
          {alert.status === "open" ? (
            <form action={acknowledgeClinicalAlertAction}>
              <input type="hidden" name="alert_id" value={alert.id} />
              <button
                type="submit"
                className="text-xs px-2.5 py-1 rounded bg-zinc-900 text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
              >
                Acknowledge
              </button>
            </form>
          ) : null}
          {alert.status !== "resolved" ? (
            <details>
              <summary className="text-xs px-2.5 py-1 rounded border border-current cursor-pointer list-none">
                Resolve
              </summary>
              <form
                action={resolveClinicalAlertAction}
                className="mt-2 flex flex-col gap-2 bg-white dark:bg-zinc-900 p-2 rounded border border-zinc-200 dark:border-zinc-800 min-w-[200px]"
              >
                <input type="hidden" name="alert_id" value={alert.id} />
                <textarea
                  name="notes"
                  placeholder="Resolution notes (optional)"
                  className="text-sm rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1.5"
                  rows={2}
                />
                <button
                  type="submit"
                  className="text-xs px-2.5 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700"
                >
                  Mark resolved
                </button>
              </form>
            </details>
          ) : alert.resolution_notes ? (
            <span
              className="text-xs italic text-zinc-600 dark:text-zinc-400"
              title={alert.resolution_notes}
            >
              note: {alert.resolution_notes.slice(0, 60)}
              {alert.resolution_notes.length > 60 ? "…" : ""}
            </span>
          ) : null}
        </div>
      </footer>
    </li>
  );
}

function PagingStatus({ alert }: { alert: ClinicalAlert }) {
  // Three states worth distinguishing:
  //   1. never paged (paged_attempts == 0): show "Pending page" hint
  //   2. paged with a doctor: show who + when + attempt count
  //   3. paged but no doctor reachable: show ⚠️ red — operator
  //      action needed (assign on-call doctor, or page manually)
  // We don't render this section at all for non-critical
  // alerts (high severity gets queued but never auto-paged).
  const exhausted = alert.paged_attempts >= MAX_PAGE_ATTEMPTS;
  const repagingActive =
    alert.status === "open" &&
    !exhausted &&
    alert.paged_attempts > 0;

  let line: React.ReactNode;
  let tone: string;

  if (alert.paged_attempts === 0) {
    line = "Page pending — dispatcher will pick it up shortly.";
    tone = "text-zinc-600 dark:text-zinc-400";
  } else if (alert.paged_doctor_id !== null) {
    line = (
      <>
        Paged doctor #{alert.paged_doctor_id}
        {alert.paged_at ? ` at ${formatRelative(alert.paged_at)}` : null}
        {" "}· attempt {alert.paged_attempts}/{MAX_PAGE_ATTEMPTS}
      </>
    );
    tone = exhausted
      ? "text-red-700 dark:text-red-300"
      : repagingActive
        ? "text-amber-700 dark:text-amber-300"
        : "text-emerald-700 dark:text-emerald-300";
  } else {
    line = (
      <>
        ⚠️ No doctor reachable on attempt {alert.paged_attempts}/
        {MAX_PAGE_ATTEMPTS}. Set a doctor on-call or page manually.
      </>
    );
    tone = "text-red-700 dark:text-red-300";
  }

  return (
    <div className="mt-3 flex items-baseline justify-between gap-3 flex-wrap">
      <div className={`text-xs font-medium ${tone}`}>{line}</div>
      {alert.status === "open" && !exhausted ? (
        <form action={pageClinicalAlertAction}>
          <input type="hidden" name="alert_id" value={alert.id} />
          <button
            type="submit"
            className="text-xs px-2 py-0.5 rounded border border-current hover:bg-white/40 dark:hover:bg-zinc-900/40"
            title="Trigger another paging attempt now"
          >
            Page again
          </button>
        </form>
      ) : null}
    </div>
  );
}
