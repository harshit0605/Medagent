import Link from "next/link";

import { orchestrator } from "@/lib/backend";

export const dynamic = "force-dynamic";

const PRESET_DAYS = [1, 7, 30] as const;

function formatUsd(micros: number): string {
  /**
   * Convert USD micros (10⁻⁶ USD integer) to a display string.
   * Uses 4 decimal places below $1 so the cheapest call kinds
   * (gpt-4o-mini at fractions of a cent per call) read sensibly.
   */
  const dollars = micros / 1_000_000;
  if (dollars >= 1) return `$${dollars.toFixed(2)}`;
  if (dollars >= 0.01) return `$${dollars.toFixed(4)}`;
  return `$${dollars.toFixed(6)}`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatLatency(ms: number | null): string {
  if (ms === null) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms}ms`;
}

export default async function LlmCostAnalyticsPage({
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
    analytics = await orchestrator.getLlmCostAnalytics({ days });
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !analytics) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load LLM cost analytics.{" "}
        <code className="font-mono text-xs">{error}</code>
      </div>
    );
  }

  // Latency tone: p95 above 5s is concerning for a real-time
  // chat UX; above 10s is bad. Tile colour reflects this so a
  // glance tells you whether you need to drill in.
  const p95 = analytics.latency.p95_ms ?? 0;
  const latencyTone =
    p95 > 10000
      ? "red"
      : p95 > 5000
        ? "amber"
        : undefined;

  // Daily-rate projection: scaling current spend out to a
  // 30-day month gives ops a "what's our monthly burn?" number.
  const dailyAvgUsd = analytics.total_cost_usd_micros / days / 1_000_000;
  const monthlyProjectionUsd = dailyAvgUsd * 30;

  return (
    <div className="space-y-8">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">
            LLM cost &amp; latency
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            Per-call OpenAI telemetry. Drives &quot;are we
            operating at acceptable cost?&quot; before scaling.
          </p>
        </div>
        <div className="flex items-center gap-1 text-xs">
          <span className="text-zinc-500 mr-1">window:</span>
          {PRESET_DAYS.map((preset) => (
            <Link
              key={preset}
              href={`/analytics/llm-cost?days=${preset}`}
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

      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryTile
          label="Total cost"
          value={formatUsd(analytics.total_cost_usd_micros)}
          hint={`${days}d window`}
        />
        <SummaryTile
          label="Monthly run-rate"
          value={`$${monthlyProjectionUsd.toFixed(2)}`}
          hint="extrapolated from window"
        />
        <SummaryTile
          label="Total tokens"
          value={formatTokens(analytics.total_tokens)}
          hint={`${analytics.total_calls.toLocaleString()} calls`}
        />
        <SummaryTile
          label="Errors"
          value={String(analytics.errors_count)}
          hint="failed LLM calls"
          tone={analytics.errors_count > 0 ? "amber" : undefined}
        />
      </section>

      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryTile
          label="Latency p50"
          value={formatLatency(analytics.latency.p50_ms)}
          hint="median per call"
        />
        <SummaryTile
          label="Latency p95"
          value={formatLatency(analytics.latency.p95_ms)}
          hint="95th percentile"
          tone={latencyTone}
        />
        <SummaryTile
          label="Latency p99"
          value={formatLatency(analytics.latency.p99_ms)}
          hint="99th percentile"
        />
        <SummaryTile
          label="Mean"
          value={formatLatency(analytics.latency.mean_ms)}
          hint="average per call"
        />
      </section>

      {analytics.total_calls === 0 ? (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          No LLM calls recorded in this window. Tracking starts at
          the next inbound message that triggers an LLM path.
        </section>
      ) : (
        <>
          <section>
            <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
              By call kind
            </h2>
            <p className="text-xs text-zinc-500 mt-1">
              Which code path is driving spend. ``compose_reply``
              + ``recap_generate`` are typically the biggest
              contributors.
            </p>
            <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
                  <tr>
                    <th className="text-left px-3 py-2">Call kind</th>
                    <th className="text-right px-3 py-2 w-20">Calls</th>
                    <th className="text-right px-3 py-2 w-24">Tokens</th>
                    <th className="text-right px-3 py-2 w-28">Cost</th>
                    <th className="text-right px-3 py-2 w-24">% of total</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                  {analytics.by_call_kind.map((row) => {
                    const pct =
                      analytics.total_cost_usd_micros > 0
                        ? (row.cost_usd_micros /
                            analytics.total_cost_usd_micros) *
                          100
                        : 0;
                    return (
                      <tr key={row.call_kind}>
                        <td className="px-3 py-2 font-mono text-[11px]">
                          {row.call_kind}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {row.calls.toLocaleString()}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {formatTokens(row.tokens)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {formatUsd(row.cost_usd_micros)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-zinc-500">
                          {pct.toFixed(1)}%
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section>
            <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
              By model
            </h2>
            <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {analytics.by_model.map((m) => (
                <div
                  key={m.model}
                  className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4"
                >
                  <div className="text-xs uppercase tracking-wide text-zinc-500 font-mono">
                    {m.model}
                  </div>
                  <div className="text-xl font-semibold mt-1">
                    {formatUsd(m.cost_usd_micros)}
                  </div>
                  <div className="text-xs text-zinc-500 mt-1">
                    {m.calls.toLocaleString()} calls ·{" "}
                    {formatTokens(m.tokens)} tokens
                  </div>
                </div>
              ))}
            </div>
          </section>

          {analytics.top_patients.length > 0 ? (
            <section>
              <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
                Top patients by cost
              </h2>
              <p className="text-xs text-zinc-500 mt-1">
                Heavy LLM consumers — usually long booking
                conversations. Useful for finding patient flows
                that loop unnecessarily.
              </p>
              <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
                    <tr>
                      <th className="text-left px-3 py-2">Patient</th>
                      <th className="text-right px-3 py-2 w-20">Calls</th>
                      <th className="text-right px-3 py-2 w-24">Tokens</th>
                      <th className="text-right px-3 py-2 w-28">Cost</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                    {analytics.top_patients.map((p) => (
                      <tr key={p.patient_id}>
                        <td className="px-3 py-2 font-mono text-[11px]">
                          {p.patient_id}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {p.calls.toLocaleString()}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {formatTokens(p.tokens)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {formatUsd(p.cost_usd_micros)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}
        </>
      )}
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
  value: string;
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
