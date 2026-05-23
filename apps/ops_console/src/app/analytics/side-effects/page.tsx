import Link from "next/link";

import { orchestrator } from "@/lib/backend";

export const dynamic = "force-dynamic";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

const PRESET_DAYS = [7, 30, 90] as const;

export default async function SideEffectAnalyticsPage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const params = await searchParams;
  const days = (() => {
    const raw = parseInt(params.days ?? "30", 10);
    if (!Number.isFinite(raw) || raw < 1 || raw > 365) return 30;
    return raw;
  })();

  let analytics;
  let error: string | null = null;
  try {
    analytics = await orchestrator.getSideEffectAnalytics({ days });
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !analytics) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load side-effect analytics.{" "}
        <code className="font-mono text-xs">{error}</code>
      </div>
    );
  }

  // Max symptom count drives the bar widths so the longest bar
  // anchors the chart and shorter bars scale relative to it.
  const maxSymptomCount = Math.max(
    1,
    ...analytics.top_symptoms.map((s) => s.count),
  );

  return (
    <div className="space-y-8">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">
            Side-effect frequency
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            Clinical pattern detection across the panel.{" "}
            {formatDate(analytics.since)} →{" "}
            {formatDate(analytics.until)} ({days}d window)
          </p>
        </div>
        <div className="flex items-center gap-1 text-xs">
          <span className="text-zinc-500 mr-1">window:</span>
          {PRESET_DAYS.map((preset) => (
            <Link
              key={preset}
              href={`/analytics/side-effects?days=${preset}`}
              className={
                "px-2 py-0.5 rounded border " +
                (preset === days
                  ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                  : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
              }
            >
              {preset}d
            </Link>
          ))}
        </div>
      </div>

      {/* Summary tiles. */}
      <section className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <SummaryTile
          label="Total reports"
          value={analytics.total_reports}
          hint={`in last ${days}d`}
        />
        <SummaryTile
          label="Unique patients"
          value={analytics.unique_patients}
          hint="reporting at least once"
        />
        <SummaryTile
          label="Medications mentioned"
          value={analytics.unique_medications}
          hint="cross-referenced to active regimens"
        />
      </section>

      {analytics.total_reports === 0 ? (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          No side-effect reports in the selected window.
        </section>
      ) : (
        <>
          {/* Per-medication breakdown — typically the most actionable
              section. A single medication driving multiple reports
              is the strongest signal of a clinical issue. */}
          <section>
            <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
              By medication
            </h2>
            <p className="text-xs text-zinc-500 mt-1">
              Reports cross-referenced against the patient&apos;s
              active regimens at report time. Reports without a
              mentioned medication contribute to the symptom +
              cohort rollups but not this section.
            </p>
            {analytics.by_medication.length === 0 ? (
              <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
                No reports attributed to specific medications in this
                window.
              </div>
            ) : (
              <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
                    <tr>
                      <th className="text-left px-3 py-2">
                        Medication
                      </th>
                      <th className="text-right px-3 py-2 w-24">
                        Reports
                      </th>
                      <th className="text-right px-3 py-2 w-24">
                        Patients
                      </th>
                      <th className="text-left px-3 py-2">
                        Top symptoms
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                    {analytics.by_medication.map((med) => (
                      <tr key={med.medication_name}>
                        <td className="px-3 py-2 font-medium">
                          {med.medication_name}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {med.report_count}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {med.patient_count}
                        </td>
                        <td className="px-3 py-2">
                          {med.top_symptoms.length === 0 ? (
                            <span className="text-zinc-400 text-xs">
                              —
                            </span>
                          ) : (
                            <div className="flex flex-wrap gap-1">
                              {med.top_symptoms.map(([sym, cnt]) => (
                                <span
                                  key={sym}
                                  className="px-1.5 py-0.5 rounded text-[11px] bg-zinc-100 dark:bg-zinc-800"
                                >
                                  {sym} <span className="text-zinc-500">×{cnt}</span>
                                </span>
                              ))}
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Per-cohort breakdown — surfaces "diabetics are reporting
              twice as often as cardiac patients". */}
          <section>
            <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
              By cohort
            </h2>
            <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              {analytics.by_cohort.map((cohort) => (
                <div
                  key={cohort.cohort}
                  className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4"
                >
                  <div className="text-xs uppercase tracking-wide text-zinc-500">
                    {cohort.cohort.replace(/_/g, " ")}
                  </div>
                  <div className="text-2xl font-semibold mt-1">
                    {cohort.report_count}
                  </div>
                  <div className="text-xs text-zinc-500">
                    {cohort.patient_count} patient(s)
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* Top symptoms across panel — horizontal bar chart so the
              relative magnitudes are scannable. Each bar's width
              relative to the max anchors the comparison. */}
          <section>
            <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
              Top symptoms (panel-wide)
            </h2>
            <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 space-y-1.5">
              {analytics.top_symptoms.map((sym) => {
                const widthPct = Math.max(
                  4,
                  Math.round((sym.count / maxSymptomCount) * 100),
                );
                return (
                  <div
                    key={sym.symptom}
                    className="flex items-center gap-3 text-xs"
                  >
                    <div className="w-32 text-right text-zinc-700 dark:text-zinc-300 capitalize tabular-nums">
                      {sym.symptom}
                    </div>
                    <div className="flex-1 h-5 bg-zinc-100 dark:bg-zinc-800 rounded-sm overflow-hidden">
                      <div
                        className="h-full bg-rose-500 dark:bg-rose-600"
                        style={{ width: `${widthPct}%` }}
                      />
                    </div>
                    <div className="w-12 text-zinc-700 dark:text-zinc-300 tabular-nums">
                      {sym.count}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function SummaryTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: number;
  hint: string;
}) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className="text-2xl font-semibold">{value}</div>
      <div className="text-xs text-zinc-500 mt-1">{label}</div>
      <div className="text-[10px] text-zinc-400 mt-0.5">{hint}</div>
    </div>
  );
}
