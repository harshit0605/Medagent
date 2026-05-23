import Link from "next/link";

import { orchestrator } from "@/lib/backend";

import { createBroadcastCampaignAction } from "./_actions";

export const dynamic = "force-dynamic";

const COHORTS = ["diabetes", "cardiac", "fall_risk"] as const;

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const diff = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(diff);
  if (abs < 60) return `${abs}s ago`;
  if (abs < 3600) return `${Math.round(abs / 60)}m ago`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h ago`;
  return `${Math.round(abs / 86400)}d ago`;
}

export default async function CampaignsPage() {
  let campaigns;
  let error: string | null = null;
  try {
    campaigns = await orchestrator.listBroadcastCampaigns({
      limit: 100,
    });
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Broadcast campaigns</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Send a template to a cohort of patients. Each campaign
          materialises into N scheduled events; the dispatcher
          carries each individual send through retry + consent
          gates + delivery tracking.
        </p>
      </div>

      {/* Create form. Server action handles validation +
          materialisation + redirect to the new campaign's
          detail page. */}
      <details className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
        <summary className="text-sm font-medium cursor-pointer text-zinc-700 dark:text-zinc-200">
          New campaign
        </summary>
        <form
          action={createBroadcastCampaignAction}
          className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs"
        >
          <label className="block">
            <span className="block text-zinc-500 mb-1">
              Name <span className="text-red-500">*</span>
            </span>
            <input
              type="text"
              name="name"
              required
              placeholder="e.g. Flu shot reminder Q4 2026"
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1.5 text-sm"
            />
          </label>
          <label className="block">
            <span className="block text-zinc-500 mb-1">
              Cohort <span className="text-red-500">*</span>
            </span>
            <select
              name="cohort"
              required
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1.5 text-sm"
            >
              <option value="">— pick one —</option>
              {COHORTS.map((c) => (
                <option key={c} value={c}>
                  {c.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="block sm:col-span-2">
            <span className="block text-zinc-500 mb-1">
              Template name (Meta-approved){" "}
              <span className="text-red-500">*</span>
            </span>
            <input
              type="text"
              name="template_name"
              required
              placeholder="e.g. seasonal_flu_v1"
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1.5 text-sm font-mono"
            />
          </label>
          <label className="block sm:col-span-2">
            <span className="block text-zinc-500 mb-1">
              Template params (JSON, optional)
            </span>
            <textarea
              name="template_params"
              rows={3}
              placeholder='{"1_name": "{{patient.first_name}}"}'
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1.5 text-xs font-mono"
            />
          </label>
          <label className="block sm:col-span-2">
            <span className="block text-zinc-500 mb-1">
              Notes (operator memo, optional)
            </span>
            <input
              type="text"
              name="notes"
              placeholder="why this campaign, who approved, etc."
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1.5 text-sm"
            />
          </label>
          <input type="hidden" name="created_by" defaultValue="ops" />
          <div className="sm:col-span-2 flex justify-between items-center">
            <span className="text-[11px] text-zinc-500">
              Materialises immediately — opt-out / paused / erased
              patients are excluded with a reason.
            </span>
            <button
              type="submit"
              className="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-700 text-white"
            >
              Create &amp; send
            </button>
          </div>
        </form>
      </details>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t list campaigns.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : !campaigns || campaigns.length === 0 ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          No campaigns yet. Create one above.
        </div>
      ) : (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
              <tr>
                <th className="text-left px-3 py-2">Name</th>
                <th className="text-left px-3 py-2 w-28">Cohort</th>
                <th className="text-left px-3 py-2 w-44">Template</th>
                <th className="text-right px-3 py-2 w-20">Sent</th>
                <th className="text-right px-3 py-2 w-20">Skipped</th>
                <th className="text-left px-3 py-2 w-28">Status</th>
                <th className="text-left px-3 py-2 w-28">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
              {campaigns.map((c) => {
                const cohort =
                  (c.cohort_filter as { cohort?: string }).cohort ??
                  "—";
                return (
                  <tr key={c.id}>
                    <td className="px-3 py-2">
                      <Link
                        href={`/campaigns/${c.id}`}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {c.name}
                      </Link>
                    </td>
                    <td className="px-3 py-2 text-xs text-zinc-600 dark:text-zinc-400 capitalize">
                      {cohort.replace(/_/g, " ")}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px]">
                      {c.template_name}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {c.sent_count}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {c.skipped_count > 0 ? (
                        <span className="text-amber-700 dark:text-amber-400">
                          {c.skipped_count}
                        </span>
                      ) : (
                        c.skipped_count
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      <span
                        className={
                          "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                          (c.status === "materialised"
                            ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
                            : c.status === "draft"
                              ? "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                              : "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200")
                        }
                      >
                        {c.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs text-zinc-500">
                      {formatRelative(c.created_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
