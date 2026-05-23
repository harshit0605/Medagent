import Link from "next/link";

import { orchestrator, type AnalyticsSnapshot } from "@/lib/backend";

export const dynamic = "force-dynamic";

/** Inline-SVG sparkline. Renders a line + filled area for the
 * primary series. Auto-scales to the max value seen. Tooltips on
 * each point so you can hover the dot for an exact daily reading
 * without depending on a chart library. */
function Sparkline({
  values,
  labels,
  height = 48,
  width = 360,
  stroke = "#10b981",
  fill = "rgba(16, 185, 129, 0.15)",
  formatValue = (v: number) => v.toFixed(1),
  unit = "",
}: {
  values: number[];
  labels: string[];
  height?: number;
  width?: number;
  stroke?: string;
  fill?: string;
  formatValue?: (v: number) => string;
  unit?: string;
}) {
  if (values.length === 0) {
    return (
      <div className="text-[11px] text-zinc-500 italic">No data.</div>
    );
  }
  const max = Math.max(1, ...values);
  const min = Math.min(0, ...values);
  const range = max - min || 1;
  const padX = 2;
  const padY = 4;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;
  const stepX = values.length === 1 ? 0 : innerW / (values.length - 1);

  const points: { x: number; y: number; v: number; label: string }[] =
    values.map((v, i) => {
      const x = padX + stepX * i;
      const y =
        padY + innerH - ((v - min) / range) * innerH;
      return { x, y, v, label: labels[i] ?? "" };
    });

  const pointsAttr = points.map((p) => `${p.x},${p.y}`).join(" ");
  const areaPathParts: string[] = [
    `M ${points[0].x} ${padY + innerH}`,
    `L ${points[0].x} ${points[0].y}`,
  ];
  for (let i = 1; i < points.length; i++) {
    areaPathParts.push(`L ${points[i].x} ${points[i].y}`);
  }
  areaPathParts.push(
    `L ${points[points.length - 1].x} ${padY + innerH}`,
    "Z",
  );

  const last = points[points.length - 1];
  return (
    <div className="space-y-1">
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="block"
        role="img"
        aria-label={`Sparkline with ${values.length} daily points`}
      >
        <path d={areaPathParts.join(" ")} fill={fill} />
        <polyline
          points={pointsAttr}
          fill="none"
          stroke={stroke}
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {points.map((p, i) => (
          <circle
            key={i}
            cx={p.x}
            cy={p.y}
            r={1.5}
            fill={stroke}
          >
            <title>
              {p.label}: {formatValue(p.v)}
              {unit}
            </title>
          </circle>
        ))}
      </svg>
      <div className="flex items-center justify-between text-[10px] text-zinc-500 tabular-nums">
        <span>{labels[0]}</span>
        <span className="font-medium text-zinc-700 dark:text-zinc-300">
          last: {formatValue(last.v)}
          {unit}
        </span>
        <span>{labels[labels.length - 1]}</span>
      </div>
    </div>
  );
}

const VALID_WINDOWS = [7, 30, 90] as const;
type Window = (typeof VALID_WINDOWS)[number];

const CATEGORY_LABEL: Record<string, string> = {
  clinical_question: "Clinical",
  administrative: "Admin",
  billing: "Billing",
  scheduling: "Scheduling",
  faq: "FAQ",
  social: "Social",
  unsafe: "Unsafe",
  action_tap: "Tap",
  unknown: "Unknown",
};

const URGENCY_LABEL: Record<string, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

const INPUT_KIND_LABEL: Record<string, string> = {
  text: "💬 text",
  voice: "🎤 voice",
  image: "📷 image",
  button: "🔘 button",
};

const URGENCY_BAR_COLOR: Record<string, string> = {
  critical: "bg-red-500",
  high: "bg-orange-500",
  medium: "bg-yellow-500",
  low: "bg-zinc-400",
};

const CATEGORY_BAR_COLOR: Record<string, string> = {
  clinical_question: "bg-indigo-500",
  administrative: "bg-zinc-500",
  billing: "bg-amber-500",
  scheduling: "bg-blue-500",
  faq: "bg-emerald-500",
  social: "bg-zinc-400",
  unsafe: "bg-red-500",
  action_tap: "bg-zinc-400",
  unknown: "bg-zinc-400",
};

const PRIORITY_BAR_COLOR: Record<string, string> = {
  p0: "bg-red-500",
  p1: "bg-orange-500",
  p2: "bg-yellow-500",
  p3: "bg-zinc-400",
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

function formatMinutes(min: number | null): string {
  if (min === null) return "—";
  if (min < 60) return `${min.toFixed(1)}m`;
  if (min < 1440) return `${(min / 60).toFixed(1)}h`;
  return `${(min / 1440).toFixed(1)}d`;
}

/** Compact bar visualization. ``parts`` is rendered as a single
 * stacked bar; the legend below it shows label + count + percentage.
 * Total is computed if not provided so callers don't have to. */
function StackedBar({
  parts,
  total,
}: {
  parts: { label: string; count: number; color: string; key: string }[];
  total?: number;
}) {
  const sum = total ?? parts.reduce((a, p) => a + p.count, 0);
  if (sum === 0) {
    return (
      <div className="text-xs text-zinc-500 italic">
        No data in this window.
      </div>
    );
  }
  const sortedParts = [...parts].sort((a, b) => b.count - a.count);
  return (
    <div className="space-y-2">
      <div className="h-3 rounded-full overflow-hidden flex bg-zinc-100 dark:bg-zinc-800">
        {sortedParts.map((p) => {
          if (p.count === 0) return null;
          const width = (p.count / sum) * 100;
          return (
            <div
              key={p.key}
              className={p.color}
              style={{ width: `${width}%` }}
              title={`${p.label}: ${p.count} (${((p.count / sum) * 100).toFixed(1)}%)`}
            />
          );
        })}
      </div>
      <ul className="text-xs space-y-0.5">
        {sortedParts.map((p) => {
          if (p.count === 0) return null;
          return (
            <li key={p.key} className="flex items-center gap-2">
              <span
                className={"w-2 h-2 rounded-sm " + p.color}
                aria-hidden="true"
              />
              <span className="flex-1">{p.label}</span>
              <span className="tabular-nums text-zinc-500">{p.count}</span>
              <span className="tabular-nums text-zinc-400 w-12 text-right">
                {((p.count / sum) * 100).toFixed(1)}%
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function MetricCard({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "warn" | "danger" | "good";
}) {
  const valueClass =
    tone === "danger"
      ? "text-red-600 dark:text-red-400"
      : tone === "warn"
        ? "text-amber-600 dark:text-amber-400"
        : tone === "good"
          ? "text-emerald-600 dark:text-emerald-400"
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

export default async function AnalyticsPage({
  searchParams,
}: {
  searchParams: Promise<{ window?: string }>;
}) {
  const params = await searchParams;
  const requested = Number(params.window ?? 30);
  const w: Window = (VALID_WINDOWS as readonly number[]).includes(requested)
    ? (requested as Window)
    : 30;

  let snapshot: AnalyticsSnapshot | null = null;
  let error: string | null = null;
  try {
    snapshot = await orchestrator.analytics(w);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !snapshot) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-semibold">Analytics</h1>
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t reach <code>/ops/analytics</code>.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      </div>
    );
  }

  const adh = snapshot.adherence;
  const funnel = snapshot.recap_funnel;
  const inbox = snapshot.inbox;
  const queue = snapshot.ops_queue;
  const ts = snapshot.timeseries;

  // Day labels — short MM-DD form for sparkline endpoints. The full
  // date is in the per-point title tooltip.
  const dayLabels = ts.adherence.map((b) => {
    const d = new Date(b.date);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  });
  const adherenceRates = ts.adherence.map((b) => b.rate * 100);
  const inboxTotals = ts.inbox.map((b) => b.total);
  const inboxHighCriticalTotals = ts.inbox.map(
    (b) => b.critical + b.high,
  );
  const recapSent = ts.recap.map((b) => b.sent);
  const recapAcked = ts.recap.map((b) => b.acked);
  const ticketsOpened = ts.tickets.map((b) => b.opened);
  const ticketsResolved = ts.tickets.map((b) => b.resolved);

  // Tone choices: green if doing well, red if poor, amber when middling.
  // These thresholds are V1 placeholders — wire them to clinician-set
  // targets later.
  const adherenceTone =
    adh.rate >= 0.85 ? "good" : adh.rate >= 0.7 ? "warn" : "danger";
  const ackTone =
    funnel.ack_rate >= 0.7 ? "good" : funnel.ack_rate >= 0.4 ? "warn" : "danger";

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <h1 className="text-xl font-semibold">Analytics</h1>
        <div className="flex gap-1 text-sm">
          {VALID_WINDOWS.map((d) => {
            const active = d === w;
            return (
              <Link
                key={d}
                href={d === 30 ? "/analytics" : `/analytics?window=${d}`}
                className={
                  "px-3 py-1 rounded border " +
                  (active
                    ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                    : "border-zinc-200 dark:border-zinc-800 text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100")
                }
              >
                {d}d
              </Link>
            );
          })}
        </div>
      </div>

      <p className="text-sm text-zinc-500 max-w-2xl">
        Program-level outcome snapshot — last {w} days. Headline tiles
        + daily sparklines so you can see both &quot;where are we now?&quot;
        and &quot;how have we trended?&quot; in one view. Hover any
        sparkline point for the exact daily reading.
      </p>

      {/* Cross-links to the per-domain analytics pages. Each
          page lives on its own URL with its own window controls
          rather than crowding this overview. */}
      <div className="flex flex-wrap gap-2 text-xs">
        <Link
          href="/analytics/side-effects"
          className="px-2.5 py-1 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          Side-effect frequency →
        </Link>
        <Link
          href="/analytics/llm-cost"
          className="px-2.5 py-1 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          LLM cost &amp; latency →
        </Link>
      </div>

      {/* Adherence */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Adherence
        </h2>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard
            label={`Adherence (${w}d)`}
            value={pct(adh.rate)}
            hint={`${adh.taken} taken / ${adh.taken + adh.missed + adh.skipped} matured`}
            tone={adherenceTone}
          />
          <MetricCard
            label="Doses missed"
            value={String(adh.missed)}
            hint={`+${adh.skipped} skipped`}
            tone={adh.missed > 0 ? "warn" : undefined}
          />
          <MetricCard
            label="Doses scheduled"
            value={String(adh.scheduled)}
            hint="Awaiting confirmation"
          />
          <MetricCard
            label="Total events"
            value={String(adh.total)}
            hint="Including future-dated"
          />
        </div>
        <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
            Daily adherence rate (%)
          </div>
          <Sparkline
            values={adherenceRates}
            labels={dayLabels}
            stroke="#10b981"
            fill="rgba(16, 185, 129, 0.15)"
            formatValue={(v) => v.toFixed(1)}
            unit="%"
          />
        </div>
      </section>

      {/* Recap funnel */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Recap funnel
        </h2>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard
            label={`Recaps sent (${w}d)`}
            value={String(funnel.sent_total)}
            hint={`${funnel.draft} drafts in flight`}
          />
          <MetricCard
            label="Acked"
            value={String(funnel.acknowledged)}
            hint="Patient confirmed receipt"
            tone="good"
          />
          <MetricCard
            label="Questioned"
            value={String(funnel.questioned)}
            hint="Routed to ops"
            tone={funnel.questioned > 0 ? "warn" : undefined}
          />
          <MetricCard
            label="Ack rate"
            value={pct(funnel.ack_rate)}
            hint="acknowledged / sent"
            tone={ackTone}
          />
        </div>
        <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
              Daily recaps sent
            </div>
            <Sparkline
              values={recapSent}
              labels={dayLabels}
              stroke="#3b82f6"
              fill="rgba(59, 130, 246, 0.15)"
              formatValue={(v) => v.toFixed(0)}
            />
          </div>
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
              Daily recaps acked
            </div>
            <Sparkline
              values={recapAcked}
              labels={dayLabels}
              stroke="#10b981"
              fill="rgba(16, 185, 129, 0.15)"
              formatValue={(v) => v.toFixed(0)}
            />
          </div>
        </div>
      </section>

      {/* Daily inbox volume */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Daily inbox volume
        </h2>
        <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
              All inbound
            </div>
            <Sparkline
              values={inboxTotals}
              labels={dayLabels}
              stroke="#6366f1"
              fill="rgba(99, 102, 241, 0.15)"
              formatValue={(v) => v.toFixed(0)}
            />
          </div>
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
              High + critical urgency only
            </div>
            <Sparkline
              values={inboxHighCriticalTotals}
              labels={dayLabels}
              stroke="#ef4444"
              fill="rgba(239, 68, 68, 0.15)"
              formatValue={(v) => v.toFixed(0)}
            />
          </div>
        </div>
      </section>

      {/* Inbox composition */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Inbox by category
          </h2>
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <StackedBar
              parts={Object.entries(inbox.by_category).map(([k, v]) => ({
                key: k,
                label: CATEGORY_LABEL[k] ?? k,
                count: v,
                color: CATEGORY_BAR_COLOR[k] ?? "bg-zinc-400",
              }))}
            />
          </div>
        </div>
        <div>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Inbox by urgency
          </h2>
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <StackedBar
              parts={Object.entries(inbox.by_urgency).map(([k, v]) => ({
                key: k,
                label: URGENCY_LABEL[k] ?? k,
                count: v,
                color: URGENCY_BAR_COLOR[k] ?? "bg-zinc-400",
              }))}
            />
          </div>
        </div>
        <div>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Inbox by input kind
          </h2>
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <StackedBar
              parts={Object.entries(inbox.by_input_kind).map(([k, v]) => ({
                key: k,
                label: INPUT_KIND_LABEL[k] ?? k,
                count: v,
                color: "bg-purple-400",
              }))}
            />
          </div>
        </div>
      </section>

      {/* Ops queue */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Ops queue
        </h2>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard
            label="Open tickets"
            value={String(queue.open_total)}
            hint="open + acknowledged"
            tone={queue.open_total > 5 ? "warn" : undefined}
          />
          <MetricCard
            label={`Opened (${w}d)`}
            value={String(queue.opened_in_window)}
          />
          <MetricCard
            label={`Resolved (${w}d)`}
            value={String(queue.resolved_in_window)}
            tone="good"
          />
          <MetricCard
            label="Median resolve"
            value={formatMinutes(queue.median_resolve_minutes)}
            hint={
              queue.resolved_in_window > 0
                ? `Across ${queue.resolved_in_window} resolutions`
                : "No resolutions in window"
            }
          />
        </div>
        <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-xs uppercase tracking-wide text-zinc-500 mb-2">
            Open by priority
          </div>
          <StackedBar
            parts={Object.entries(queue.by_priority).map(([k, v]) => ({
              key: k,
              label: k.toUpperCase(),
              count: v,
              color: PRIORITY_BAR_COLOR[k] ?? "bg-zinc-400",
            }))}
            total={queue.open_total}
          />
        </div>
        <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
              Tickets opened per day
            </div>
            <Sparkline
              values={ticketsOpened}
              labels={dayLabels}
              stroke="#f97316"
              fill="rgba(249, 115, 22, 0.15)"
              formatValue={(v) => v.toFixed(0)}
            />
          </div>
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
            <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
              Tickets resolved per day
            </div>
            <Sparkline
              values={ticketsResolved}
              labels={dayLabels}
              stroke="#10b981"
              fill="rgba(16, 185, 129, 0.15)"
              formatValue={(v) => v.toFixed(0)}
            />
          </div>
        </div>
      </section>

      <div className="text-[11px] text-zinc-400">
        Window: from {formatDateTime(snapshot.since)} to now ·{" "}
        <Link href="/health" className="hover:underline">
          service health →
        </Link>
      </div>
    </div>
  );
}
