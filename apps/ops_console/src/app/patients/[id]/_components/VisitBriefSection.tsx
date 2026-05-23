import { orchestrator, type VisitBrief } from "@/lib/backend";

import { generateVisitBriefAction } from "../_actions";

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

function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 100)}%`;
}

function adherenceTone(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "";
  if (rate < 0.6) return "text-red-700 dark:text-red-300";
  if (rate < 0.8) return "text-amber-700 dark:text-amber-300";
  return "text-emerald-700 dark:text-emerald-300";
}

export async function VisitBriefSection({
  patientId,
}: {
  patientId: number;
}) {
  let briefs: VisitBrief[] = [];
  let error: string | null = null;
  try {
    briefs = await orchestrator.listVisitBriefs(patientId, {
      limit: 5,
    });
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  // Latest is the freshest non-superseded brief if one exists,
  // otherwise the freshest of any status. The list is already
  // newest-first from the backend.
  const latest =
    briefs.find((b) => b.status !== "superseded") ?? briefs[0] ?? null;
  const earlier = briefs.filter((b) => b.id !== latest?.id);

  return (
    <section>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Visit brief
        </h2>
        <form action={generateVisitBriefAction}>
          <input type="hidden" name="patient_id" value={patientId} />
          <input type="hidden" name="window_days" value="30" />
          <button
            type="submit"
            className="text-xs px-2.5 py-1 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            {latest ? "Regenerate" : "Generate"}
          </button>
        </form>
      </div>

      {error ? (
        <div className="mt-3 rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-3 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load briefs.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : !latest ? (
        <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
          No brief yet. Click <span className="font-medium">Generate</span> to
          have the LLM compile a 30-day summary from the timeline.
        </div>
      ) : (
        <article className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 space-y-4">
          <header className="flex items-baseline justify-between flex-wrap gap-2 text-xs">
            <div className="text-zinc-500">
              Generated {formatDateTime(latest.generated_at)}
              {latest.generated_by ? (
                <>
                  {" "}
                  by{" "}
                  <span className="font-mono">{latest.generated_by}</span>
                </>
              ) : null}
              {latest.appointment_id !== null &&
              latest.generated_by === "scheduler.auto" ? (
                <span
                  className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-medium bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
                  title={`Auto-prepared for appointment #${latest.appointment_id}`}
                >
                  auto · appt #{latest.appointment_id}
                </span>
              ) : latest.appointment_id !== null ? (
                <span className="ml-2 text-zinc-400">
                  for appt #{latest.appointment_id}
                </span>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <span
                className={
                  "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                  (latest.status === "sent"
                    ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
                    : latest.status === "draft"
                      ? "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200"
                      : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300")
                }
              >
                {latest.status}
              </span>
              <span className="text-zinc-500 font-mono">
                {latest.llm_model}
              </span>
            </div>
          </header>

          {latest.error ? (
            <div className="rounded border border-red-300 bg-red-50 dark:bg-red-950/30 p-3 text-sm text-red-800 dark:text-red-200">
              <div className="font-medium">Generation failed</div>
              <code className="mt-1 block font-mono text-xs whitespace-pre-line">
                {latest.error}
              </code>
            </div>
          ) : null}

          {!latest.error ? (
            <>
              {/* Numeric tiles — exact, computed in code (not LLM) so
                  the doctor can trust the count. */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                <div className="rounded border border-zinc-200 dark:border-zinc-800 p-2.5">
                  <div
                    className={
                      "text-lg font-semibold tabular-nums " +
                      adherenceTone(latest.key_metrics.adherence_rate)
                    }
                  >
                    {formatPercent(latest.key_metrics.adherence_rate)}
                  </div>
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wide">
                    Adherence
                  </div>
                </div>
                <div className="rounded border border-zinc-200 dark:border-zinc-800 p-2.5">
                  <div className="text-lg font-semibold tabular-nums">
                    {latest.key_metrics.doses_missed ?? 0}
                  </div>
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wide">
                    Doses missed
                  </div>
                </div>
                <div className="rounded border border-zinc-200 dark:border-zinc-800 p-2.5">
                  <div
                    className={
                      "text-lg font-semibold tabular-nums " +
                      ((latest.key_metrics.side_effect_count ?? 0) > 0
                        ? "text-red-700 dark:text-red-300"
                        : "")
                    }
                  >
                    {latest.key_metrics.side_effect_count ?? 0}
                  </div>
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wide">
                    Side effects
                  </div>
                </div>
                <div className="rounded border border-zinc-200 dark:border-zinc-800 p-2.5">
                  <div className="text-lg font-semibold tabular-nums">
                    {latest.key_metrics.messages_inbound ?? 0}
                  </div>
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wide">
                    Patient msgs
                  </div>
                </div>
              </div>

              {/* Summary prose — what the LLM came up with. Limited to
                  ~600 chars so it actually reads in 30 seconds. */}
              <div>
                <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                  Summary
                </h3>
                <p className="mt-1 text-sm text-zinc-800 dark:text-zinc-200 whitespace-pre-line">
                  {latest.summary}
                </p>
              </div>

              {latest.red_flags.length > 0 ? (
                <div>
                  <h3 className="text-xs font-medium uppercase tracking-wide text-red-700 dark:text-red-300">
                    Red flags
                  </h3>
                  <ul className="mt-1 space-y-1">
                    {latest.red_flags.map((rf, idx) => (
                      <li
                        key={idx}
                        className="text-sm text-red-800 dark:text-red-200 flex gap-2"
                      >
                        <span aria-hidden>⚠️</span>
                        <span>{rf}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {latest.talking_points.length > 0 ? (
                <div>
                  <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                    Talking points
                  </h3>
                  <ul className="mt-1 space-y-1 list-disc list-inside text-sm text-zinc-800 dark:text-zinc-200">
                    {latest.talking_points.map((tp, idx) => (
                      <li key={idx}>{tp}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          ) : null}

          {earlier.length > 0 ? (
            <details className="text-xs">
              <summary className="cursor-pointer text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">
                {earlier.length} earlier brief
                {earlier.length === 1 ? "" : "s"}
              </summary>
              <ul className="mt-2 space-y-1 ml-2">
                {earlier.map((b) => (
                  <li key={b.id} className="text-zinc-500">
                    <span className="font-mono">#{b.id}</span> ·{" "}
                    {formatDateTime(b.generated_at)} ·{" "}
                    <span className="italic">{b.status}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </article>
      )}
    </section>
  );
}
