import Link from "next/link";

import {
  orchestrator,
  type ClinicalAlert,
} from "@/lib/backend";

const STATUS_TONE: Record<ClinicalAlert["status"], string> = {
  open: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
  acknowledged:
    "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  resolved:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
};

function formatRelative(iso: string): string {
  const diff = Math.round(
    (Date.now() - new Date(iso).getTime()) / 1000,
  );
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

export async function PatientClinicalAlerts({
  patientId,
}: {
  patientId: number;
}) {
  let alerts: ClinicalAlert[] = [];
  let error: string | null = null;
  try {
    alerts = await orchestrator.listPatientClinicalAlerts(
      patientId,
      { limit: 10 },
    );
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  // Open alerts always shown; acknowledged/resolved only if
  // they're recent (<= 30 days) — older history is one click
  // away on /clinical-alerts. The endpoint already returns
  // newest-first.
  const open = alerts.filter((a) => a.status === "open");
  const otherRecent = alerts.filter((a) => a.status !== "open");

  if (error) {
    return (
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Clinical alerts
        </h2>
        <div className="mt-3 rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-3 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load clinical alerts.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      </section>
    );
  }

  // Render nothing when this patient has zero alerts ever —
  // no point adding visual noise to a clean patient's page.
  if (alerts.length === 0) {
    return null;
  }

  return (
    <section>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-sm font-medium text-red-700 dark:text-red-300 uppercase tracking-wide flex items-center gap-2">
          <span>🚨 Clinical alerts</span>
          {open.length > 0 ? (
            <span className="text-zinc-400 normal-case font-normal">
              ({open.length} open)
            </span>
          ) : null}
        </h2>
        <Link
          href="/clinical-alerts"
          className="text-xs text-blue-600 hover:underline dark:text-blue-400"
        >
          all alerts →
        </Link>
      </div>

      <ol className="mt-3 space-y-2">
        {open.map((a) => (
          <AlertCompactCard key={a.id} alert={a} />
        ))}
        {otherRecent.length > 0 ? (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">
              {otherRecent.length} resolved / acknowledged
            </summary>
            <ol className="mt-2 space-y-2">
              {otherRecent.map((a) => (
                <AlertCompactCard key={a.id} alert={a} />
              ))}
            </ol>
          </details>
        ) : null}
      </ol>
    </section>
  );
}

function AlertCompactCard({ alert }: { alert: ClinicalAlert }) {
  const tone =
    alert.status === "resolved"
      ? "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 opacity-70"
      : alert.severity === "critical"
        ? "border-red-300 dark:border-red-800 bg-red-50/50 dark:bg-red-950/20"
        : "border-amber-300 dark:border-amber-800 bg-amber-50/50 dark:bg-amber-950/20";
  return (
    <li className={`rounded-lg border-2 p-3 text-sm ${tone}`}>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide">
            {alert.severity === "critical" ? "🚨 critical" : "⚠️ high"}
          </span>
          <span
            className={
              "px-1.5 py-0.5 rounded text-[10px] font-medium " +
              STATUS_TONE[alert.status]
            }
          >
            {alert.status}
          </span>
        </div>
        <span
          className="text-xs text-zinc-500 tabular-nums"
          title={new Date(alert.created_at).toISOString()}
        >
          {formatRelative(alert.created_at)}
        </span>
      </div>
      <p className="mt-1.5 text-zinc-800 dark:text-zinc-200">
        {alert.clinical_summary}
      </p>
      {alert.red_flags.length > 0 ? (
        <p className="mt-1 text-[10px] text-zinc-600 dark:text-zinc-400">
          {alert.red_flags.join(" · ")}
        </p>
      ) : null}
    </li>
  );
}
