import Link from "next/link";

import { orchestrator, type InboundClassification } from "@/lib/backend";

import { FeedbackCell } from "./_components/FeedbackCell";
import { InboxReplyForm } from "./_components/InboxReplyForm";

export const dynamic = "force-dynamic";

const CATEGORIES = [
  "clinical_question",
  "administrative",
  "billing",
  "scheduling",
  "faq",
  "social",
  "unsafe",
  "action_tap",
  "unknown",
] as const;

const CATEGORY_LABEL: Record<(typeof CATEGORIES)[number], string> = {
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

const CATEGORY_BADGE: Record<string, string> = {
  clinical_question:
    "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-200",
  administrative:
    "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
  billing:
    "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  scheduling:
    "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  faq: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  social: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
  unsafe: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  action_tap:
    "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
  unknown: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
};

const URGENCY_BADGE: Record<string, string> = {
  critical:
    "bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-200 border border-red-300 dark:border-red-800",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200",
  medium: "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-200",
  low: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
};

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const diffSec = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(diffSec);
  if (abs < 60) return `${abs}s ago`;
  if (abs < 3600) return `${Math.round(abs / 60)}m ago`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h ago`;
  return `${Math.round(abs / 86400)}d ago`;
}

const FILTER_KEYS = ["category", "urgency", "escalated", "input_kind"] as const;

const INPUT_KIND_BADGE: Record<string, { label: string; className: string }> = {
  text: {
    label: "💬 text",
    className:
      "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  },
  voice: {
    label: "🎤 voice",
    className:
      "bg-purple-100 text-purple-800 dark:bg-purple-950 dark:text-purple-200",
  },
  image: {
    label: "📷 image",
    className: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  },
  button: {
    label: "🔘 button",
    className: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
  },
};

export default async function InboxPage({
  searchParams,
}: {
  searchParams: Promise<{
    category?: string;
    urgency?: string;
    escalated?: string;
    input_kind?: string;
  }>;
}) {
  const params = await searchParams;
  const filterArgs: Parameters<typeof orchestrator.listInbox>[0] = {
    limit: 100,
  };
  if (params.category) filterArgs.category = params.category;
  if (params.urgency) filterArgs.urgency = params.urgency;
  if (params.escalated === "true") filterArgs.escalated = true;
  if (params.escalated === "false") filterArgs.escalated = false;
  if (params.input_kind) filterArgs.input_kind = params.input_kind;

  let rows: InboundClassification[] = [];
  let counts: Record<string, number> = {};
  let error: string | null = null;
  try {
    [rows, counts] = await Promise.all([
      orchestrator.listInbox(filterArgs),
      orchestrator.inboxCategoryCounts(7),
    ]);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  function urlFor(updates: Record<string, string | undefined>): string {
    const next = new URLSearchParams();
    for (const k of FILTER_KEYS) {
      if (params[k]) next.set(k, params[k] as string);
    }
    for (const [k, v] of Object.entries(updates)) {
      if (v === undefined || v === "") next.delete(k);
      else next.set(k, v);
    }
    const qs = next.toString();
    return qs ? `/inbox?${qs}` : "/inbox";
  }

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <h1 className="text-xl font-semibold">Inbox</h1>
        <p className="text-sm text-zinc-500 max-w-xl">
          Classified inbound patient messages, newest first. Action-tap
          rows are decoded button presses; everything else is freeform
          text the bot triaged with an LLM.
        </p>
      </div>

      {/* Category-count chips (last 7 days) */}
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="self-center text-zinc-500">last 7d:</span>
        <Link
          href={urlFor({ category: undefined })}
          className={
            "px-2 py-0.5 rounded border " +
            (!params.category
              ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
              : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
          }
        >
          all ({Object.values(counts).reduce((a, b) => a + b, 0)})
        </Link>
        {CATEGORIES.map((c) => {
          const count = counts[c] ?? 0;
          if (count === 0 && params.category !== c) return null;
          const active = params.category === c;
          return (
            <Link
              key={c}
              href={urlFor({ category: active ? undefined : c })}
              className={
                "px-2 py-0.5 rounded border " +
                (active
                  ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                  : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
              }
            >
              {CATEGORY_LABEL[c]} ({count})
            </Link>
          );
        })}
      </div>

      {/* Secondary filters: urgency + escalation */}
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="self-center text-zinc-500">urgency:</span>
        {(["critical", "high", "medium", "low"] as const).map((u) => {
          const active = params.urgency === u;
          return (
            <Link
              key={u}
              href={urlFor({ urgency: active ? undefined : u })}
              className={
                "px-2 py-0.5 rounded border " +
                (active
                  ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                  : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
              }
            >
              {u}
            </Link>
          );
        })}
        <span className="self-center text-zinc-500 ml-2">escalated:</span>
        <Link
          href={urlFor({ escalated: params.escalated === "true" ? undefined : "true" })}
          className={
            "px-2 py-0.5 rounded border " +
            (params.escalated === "true"
              ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
              : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
          }
        >
          yes
        </Link>
        <Link
          href={urlFor({ escalated: params.escalated === "false" ? undefined : "false" })}
          className={
            "px-2 py-0.5 rounded border " +
            (params.escalated === "false"
              ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
              : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
          }
        >
          no
        </Link>
        <span className="self-center text-zinc-500 ml-2">input:</span>
        {(["text", "voice", "image", "button"] as const).map((k) => {
          const active = params.input_kind === k;
          const badge = INPUT_KIND_BADGE[k];
          return (
            <Link
              key={k}
              href={urlFor({ input_kind: active ? undefined : k })}
              className={
                "px-2 py-0.5 rounded border " +
                (active
                  ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                  : "border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800")
              }
            >
              {badge.label}
            </Link>
          );
        })}
      </div>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t list inbox.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          No messages match this filter.
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden bg-white dark:bg-zinc-900">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
              <tr>
                <th className="text-left px-4 py-2 w-28">Time</th>
                <th className="text-left px-4 py-2 w-44">Patient</th>
                <th className="text-left px-4 py-2 w-24">Input</th>
                <th className="text-left px-4 py-2 w-32">Category</th>
                <th className="text-left px-4 py-2 w-20">Urgency</th>
                <th className="text-left px-4 py-2">Summary / message</th>
                <th className="text-left px-4 py-2 w-32">Handler</th>
                <th className="text-left px-4 py-2 w-20">Escalated</th>
                <th className="text-left px-4 py-2 w-24">Reply quality</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
              {rows.map((row) => (
                <tr
                  key={row.id}
                  className={
                    row.urgency === "critical"
                      ? "bg-red-50/30 dark:bg-red-950/10"
                      : row.escalated
                        ? "bg-amber-50/30 dark:bg-amber-950/10"
                        : ""
                  }
                >
                  <td className="px-4 py-3 text-xs text-zinc-500 align-top whitespace-nowrap">
                    {formatRelative(row.created_at)}
                  </td>
                  <td className="px-4 py-3 align-top">
                    {row.patient_db_id !== null ? (
                      <Link
                        href={`/patients/${row.patient_db_id}`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {row.patient_full_name ?? row.patient_phone}
                      </Link>
                    ) : (
                      <span className="font-mono text-xs">
                        {row.patient_phone}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 align-top">
                    {(() => {
                      const badge =
                        INPUT_KIND_BADGE[row.input_kind] ??
                        INPUT_KIND_BADGE.text;
                      return (
                        <span
                          className={
                            "inline-block px-2 py-0.5 rounded text-xs font-medium " +
                            badge.className
                          }
                          title={`Input kind: ${row.input_kind}`}
                        >
                          {badge.label}
                        </span>
                      );
                    })()}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <span
                      className={
                        "inline-block px-2 py-0.5 rounded text-xs font-medium " +
                        (CATEGORY_BADGE[row.category] ?? CATEGORY_BADGE.unknown)
                      }
                    >
                      {CATEGORY_LABEL[row.category as (typeof CATEGORIES)[number]] ??
                        row.category}
                    </span>
                  </td>
                  <td className="px-4 py-3 align-top">
                    <span
                      className={
                        "inline-block px-2 py-0.5 rounded text-xs font-medium " +
                        (URGENCY_BADGE[row.urgency] ?? URGENCY_BADGE.low)
                      }
                    >
                      {row.urgency}
                    </span>
                    {/* Clinical-alert badge — shown when the
                        slice-10 triage classifier raised a
                        ``high`` or ``critical`` alert for this
                        message. Distinct from ``urgency`` (the
                        inbox classifier's view of "how soon
                        should a human look?") — this signals
                        clinical safety specifically. Click-
                        through to /clinical-alerts. */}
                    {row.clinical_severity ? (
                      <a
                        href="/clinical-alerts"
                        className={
                          "ml-1 inline-block px-1.5 py-0.5 rounded text-[10px] font-medium border-2 " +
                          (row.clinical_severity === "critical"
                            ? "bg-red-100 text-red-900 border-red-400 dark:bg-red-950/60 dark:text-red-100 dark:border-red-700"
                            : "bg-amber-100 text-amber-900 border-amber-400 dark:bg-amber-950/50 dark:text-amber-100 dark:border-amber-700")
                        }
                        title={`Clinical alert: ${row.clinical_severity}`}
                      >
                        {row.clinical_severity === "critical"
                          ? "🚨 alert"
                          : "⚠️ alert"}
                      </a>
                    ) : null}
                  </td>
                  <td className="px-4 py-3 align-top">
                    {row.summary ? (
                      <div className="text-sm text-zinc-800 dark:text-zinc-200">
                        {row.summary}
                      </div>
                    ) : null}
                    {row.inbound_text ? (
                      <div className="mt-1 text-xs text-zinc-500 line-clamp-2">
                        &ldquo;{row.inbound_text}&rdquo;
                      </div>
                    ) : null}
                    {row.patient_db_id !== null ? (
                      <details className="mt-2 group">
                        <summary className="text-[11px] text-blue-600 dark:text-blue-400 cursor-pointer hover:underline list-none">
                          Reply to patient →
                        </summary>
                        {/* Client component: adds an "AI draft"
                            button alongside the existing Send
                            flow. Only mounts when the operator
                            opens this row's <details>. */}
                        <InboxReplyForm
                          classificationId={row.id}
                          patientDbId={row.patient_db_id}
                          inReplyToMessageId={row.message_id}
                        />
                      </details>
                    ) : null}
                  </td>
                  <td className="px-4 py-3 text-xs text-zinc-500 align-top">
                    {row.handler_used ?? "—"}
                  </td>
                  <td className="px-4 py-3 align-top text-xs">
                    {row.escalated ? (
                      row.ticket_id ? (
                        <Link
                          href={`/tickets/${row.ticket_id}`}
                          className="text-blue-600 hover:underline dark:text-blue-400"
                        >
                          #{row.ticket_id} ↗
                        </Link>
                      ) : (
                        <span className="text-amber-700 dark:text-amber-300">
                          yes
                        </span>
                      )
                    ) : (
                      <span className="text-zinc-400">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <FeedbackCell
                      classificationId={row.id}
                      initialRating={row.feedback_rating}
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
