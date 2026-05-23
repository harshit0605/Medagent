import Link from "next/link";

import { orchestrator } from "@/lib/backend";

export const dynamic = "force-dynamic";

const SKIP_REASON_LABEL: Record<string, string> = {
  opted_out: "Opted out (STOP)",
  paused: "Bot paused by ops",
  erased: "Patient data erased",
  no_phone: "No phone on file",
};

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export default async function CampaignDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const campaignId = Number(id);
  if (!Number.isFinite(campaignId)) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
        Invalid campaign id: <code>{id}</code>
      </div>
    );
  }

  let detail;
  let recipients;
  let error: string | null = null;
  try {
    [detail, recipients] = await Promise.all([
      orchestrator.getBroadcastCampaign(campaignId),
      orchestrator.listBroadcastRecipients(campaignId, {
        limit: 200,
      }),
    ]);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !detail) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load campaign.{" "}
        <code className="font-mono text-xs">{error}</code>
      </div>
    );
  }

  const { campaign, counts_by_skip_reason } = detail;
  const cohort =
    (campaign.cohort_filter as { cohort?: string }).cohort ?? "—";

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <div>
          <Link
            href="/campaigns"
            className="text-xs text-zinc-500 hover:underline"
          >
            ← all campaigns
          </Link>
          <h1 className="text-xl font-semibold mt-1">
            {campaign.name}
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            cohort:{" "}
            <span className="capitalize">
              {cohort.replace(/_/g, " ")}
            </span>{" "}
            · template:{" "}
            <code className="font-mono text-xs">
              {campaign.template_name}
            </code>{" "}
            · created by{" "}
            <span className="font-mono text-xs">
              {campaign.created_by ?? "—"}
            </span>{" "}
            · {formatDateTime(campaign.created_at)}
          </p>
        </div>
        <span
          className={
            "px-2 py-0.5 rounded text-xs font-medium self-start " +
            (campaign.status === "materialised"
              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
              : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300")
          }
        >
          {campaign.status}
        </span>
      </div>

      {campaign.notes ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 text-xs">
          <span className="text-zinc-500 uppercase tracking-wide font-medium">
            Operator notes
          </span>
          <p className="mt-1 text-zinc-700 dark:text-zinc-300 whitespace-pre-line">
            {campaign.notes}
          </p>
        </div>
      ) : null}

      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryTile
          label="Recipients (total)"
          value={campaign.total_recipients}
          hint="cohort matched"
        />
        <SummaryTile
          label="Sent"
          value={campaign.sent_count}
          hint="enqueued for dispatch"
        />
        <SummaryTile
          label="Skipped"
          value={campaign.skipped_count}
          hint="see reasons →"
          tone={campaign.skipped_count > 0 ? "amber" : undefined}
        />
        <SummaryTile
          label="Materialised"
          value={
            campaign.materialised_at
              ? formatDateTime(campaign.materialised_at)
              : "—"
          }
          hint="when ops clicked Send"
          isText
        />
      </section>

      {Object.keys(counts_by_skip_reason).length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Skip-reason breakdown
          </h2>
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3">
            {Object.entries(counts_by_skip_reason).map(
              ([reason, count]) => (
                <div
                  key={reason}
                  className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4"
                >
                  <div className="text-2xl font-semibold text-amber-700 dark:text-amber-400">
                    {count}
                  </div>
                  <div className="text-xs text-zinc-500 mt-1">
                    {SKIP_REASON_LABEL[reason] ?? reason}
                  </div>
                </div>
              ),
            )}
          </div>
        </section>
      ) : null}

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Recipients
        </h2>
        <p className="text-xs text-zinc-500 mt-1">
          Per-recipient outcomes. Pending recipients have an
          enqueued scheduled event; a status of{" "}
          <code className="font-mono text-[11px]">pending</code>{" "}
          here means the broadcast layer accepted it — actual
          delivery state lives on the linked scheduled event +
          delivery-tracking pipeline.
        </p>
        {!recipients || recipients.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            No recipients on this campaign.
          </div>
        ) : (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
                <tr>
                  <th className="text-left px-3 py-2">Patient</th>
                  <th className="text-left px-3 py-2 w-28">Status</th>
                  <th className="text-left px-3 py-2 w-44">
                    Skip reason
                  </th>
                  <th className="text-left px-3 py-2 w-32">
                    Scheduled event
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {recipients.map((r) => (
                  <tr
                    key={r.id}
                    className={
                      r.status === "skipped"
                        ? "bg-amber-50/30 dark:bg-amber-950/10"
                        : ""
                    }
                  >
                    <td className="px-3 py-2">
                      {r.patient_db_id !== null ? (
                        <Link
                          href={`/patients/${r.patient_db_id}`}
                          className="text-blue-600 hover:underline dark:text-blue-400"
                        >
                          {r.patient_id}
                        </Link>
                      ) : (
                        <span className="font-mono text-xs">
                          {r.patient_id}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={
                          "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                          (r.status === "skipped"
                            ? "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
                            : r.status === "pending"
                              ? "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200"
                              : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300")
                        }
                      >
                        {r.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {r.skip_reason ? (
                        <span className="text-amber-700 dark:text-amber-400">
                          {SKIP_REASON_LABEL[r.skip_reason] ??
                            r.skip_reason}
                        </span>
                      ) : (
                        <span className="text-zinc-400">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {r.scheduled_event_id ? (
                        <code className="font-mono">
                          #{r.scheduled_event_id}
                        </code>
                      ) : (
                        <span className="text-zinc-400">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function SummaryTile({
  label,
  value,
  hint,
  tone,
  isText,
}: {
  label: string;
  value: number | string;
  hint: string;
  tone?: "amber" | "red";
  isText?: boolean;
}) {
  const valueClass =
    tone === "red"
      ? "text-red-700 dark:text-red-300"
      : tone === "amber"
        ? "text-amber-700 dark:text-amber-300"
        : "";
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div
        className={
          (isText
            ? "text-sm font-medium tabular-nums"
            : "text-2xl font-semibold") +
          " " +
          valueClass
        }
      >
        {value}
      </div>
      <div className="text-xs text-zinc-500 mt-1">{label}</div>
      <div className="text-[10px] text-zinc-400 mt-0.5">{hint}</div>
    </div>
  );
}
