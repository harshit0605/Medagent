import Link from "next/link";

import { orchestrator, type AuditSearchResponse } from "@/lib/backend";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;

// Lightweight registry of record_type values the system writes
// today. The select uses these as suggestions; ad-hoc record_types
// from future loggers can still be queried via the URL directly.
const RECORD_TYPE_OPTIONS = [
  "policy_decision",
  "whatsapp_outbound_policy",
  "workflow_summary",
  "patient_data_export",
];

// Same idea for ``flow_action`` — common values surfaced as
// suggestions to make the form scannable.
const FLOW_ACTION_OPTIONS = ["ALLOW", "TEMPLATE_ONLY", "HOLD"];

function formatDateTime(iso: string): string {
  const dt = new Date(iso);
  return dt.toLocaleString();
}

function tone(record_type: string, flow_action: string | null): string {
  // Subtle row tinting — escalations + holds stand out so a
  // scrolling reviewer notices the abnormal rows. Keep it muted
  // for normal rows so signal isn't overwhelmed.
  if (flow_action === "HOLD") {
    return "bg-red-50/50 dark:bg-red-950/20";
  }
  if (record_type === "patient_data_export") {
    return "bg-blue-50/30 dark:bg-blue-950/10";
  }
  return "";
}

export default async function AuditSearchPage({
  searchParams,
}: {
  searchParams: Promise<{
    patient_id?: string;
    record_type?: string;
    reason_code?: string;
    flow_action?: string;
    since?: string;
    until?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;
  const pageNum = Math.max(1, parseInt(params.page ?? "1", 10) || 1);
  const offset = (pageNum - 1) * PAGE_SIZE;

  // Build URLs preserving the current filter state. Used by both
  // pagination links + the per-patient "see all rows for this
  // patient" shortcut.
  function urlFor(updates: Record<string, string | undefined>): string {
    const next = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value) next.set(key, value);
    }
    for (const [key, value] of Object.entries(updates)) {
      if (value === undefined || value === "") next.delete(key);
      else next.set(key, value);
    }
    const qs = next.toString();
    return qs ? `/audit-search?${qs}` : "/audit-search";
  }

  // Only fetch when the operator has filtered something — an
  // unfiltered query would scan the whole audit_records table
  // and is rarely what someone actually wants.
  const hasFilter = !!(
    params.patient_id ||
    params.record_type ||
    params.reason_code ||
    params.flow_action ||
    params.since ||
    params.until
  );

  let response: AuditSearchResponse | null = null;
  let error: string | null = null;
  if (hasFilter) {
    try {
      response = await orchestrator.searchAuditLog({
        patient_id: params.patient_id,
        record_type: params.record_type,
        reason_code: params.reason_code,
        flow_action: params.flow_action,
        since: params.since,
        until: params.until,
        limit: PAGE_SIZE,
        offset,
      });
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  const totalPages = response
    ? Math.max(1, Math.ceil(response.total / PAGE_SIZE))
    : 1;

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold">Audit log search</h1>
          <p className="text-sm text-zinc-500 mt-1">
            Search the audit log by patient, record type, reason
            code, flow action, or date range. Use this to debug
            &ldquo;what happened with this patient yesterday?&rdquo;
            without opening a database client.
          </p>
        </div>
      </div>

      <form method="get" className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <label className="block text-xs">
          <span className="block text-zinc-500 mb-1">Patient (phone)</span>
          <input
            type="text"
            name="patient_id"
            defaultValue={params.patient_id ?? ""}
            placeholder="91+test or DB id"
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
          />
        </label>
        <label className="block text-xs">
          <span className="block text-zinc-500 mb-1">Record type</span>
          <input
            type="text"
            name="record_type"
            list="record-type-options"
            defaultValue={params.record_type ?? ""}
            placeholder="any"
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
          />
          <datalist id="record-type-options">
            {RECORD_TYPE_OPTIONS.map((opt) => (
              <option key={opt} value={opt} />
            ))}
          </datalist>
        </label>
        <label className="block text-xs">
          <span className="block text-zinc-500 mb-1">Reason code</span>
          <input
            type="text"
            name="reason_code"
            defaultValue={params.reason_code ?? ""}
            placeholder="rate_limited, optout, …"
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
          />
        </label>
        <label className="block text-xs">
          <span className="block text-zinc-500 mb-1">Flow action</span>
          <input
            type="text"
            name="flow_action"
            list="flow-action-options"
            defaultValue={params.flow_action ?? ""}
            placeholder="any"
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
          />
          <datalist id="flow-action-options">
            {FLOW_ACTION_OPTIONS.map((opt) => (
              <option key={opt} value={opt} />
            ))}
          </datalist>
        </label>
        <label className="block text-xs">
          <span className="block text-zinc-500 mb-1">Since (UTC)</span>
          <input
            type="datetime-local"
            name="since"
            defaultValue={params.since ?? ""}
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
          />
        </label>
        <label className="block text-xs">
          <span className="block text-zinc-500 mb-1">Until (UTC)</span>
          <input
            type="datetime-local"
            name="until"
            defaultValue={params.until ?? ""}
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-sm"
          />
        </label>
        <div className="flex items-end gap-2 sm:col-span-2 lg:col-span-2">
          <button
            type="submit"
            className="px-4 py-1.5 text-sm rounded bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
          >
            Search
          </button>
          <Link
            href="/audit-search"
            className="px-4 py-1.5 text-sm rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            Clear filters
          </Link>
        </div>
      </form>

      {!hasFilter ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          Set at least one filter and click Search.
        </div>
      ) : error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Search failed.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : response && response.rows.length === 0 ? (
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-8 text-center text-sm text-zinc-500">
          No audit records match these filters.
        </div>
      ) : response ? (
        <div className="space-y-3">
          <div className="flex items-baseline justify-between text-xs text-zinc-500">
            <span>
              Showing {offset + 1}–
              {Math.min(offset + response.rows.length, response.total)} of{" "}
              {response.total.toLocaleString()}
            </span>
            <span>
              Page {pageNum} of {totalPages}
            </span>
          </div>
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden bg-white dark:bg-zinc-900">
            <table className="w-full text-xs">
              <thead className="bg-zinc-50 dark:bg-zinc-950 text-zinc-500">
                <tr>
                  <th className="text-left px-3 py-2">When (UTC)</th>
                  <th className="text-left px-3 py-2">Patient</th>
                  <th className="text-left px-3 py-2">Type</th>
                  <th className="text-left px-3 py-2">Flow</th>
                  <th className="text-left px-3 py-2">Reasons</th>
                  <th className="text-left px-3 py-2">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {response.rows.map((row) => (
                  <tr
                    key={row.id}
                    className={tone(row.record_type, row.flow_action)}
                  >
                    <td className="px-3 py-2 align-top whitespace-nowrap">
                      {formatDateTime(row.logged_at)}
                    </td>
                    <td className="px-3 py-2 align-top font-mono text-[11px]">
                      <Link
                        href={urlFor({
                          patient_id: row.patient_id,
                          page: "1",
                        })}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {row.patient_id}
                      </Link>
                    </td>
                    <td className="px-3 py-2 align-top font-mono text-[11px]">
                      {row.record_type}
                    </td>
                    <td className="px-3 py-2 align-top font-mono text-[11px]">
                      {row.flow_action ?? "—"}
                    </td>
                    <td className="px-3 py-2 align-top">
                      <div className="flex flex-wrap gap-1">
                        {row.reason_codes.map((code) => (
                          <code
                            key={code}
                            className="px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 text-[10px]"
                          >
                            {code}
                          </code>
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-2 align-top">
                      {Object.keys(row.details).length === 0 ? (
                        <span className="text-zinc-400">—</span>
                      ) : (
                        <details>
                          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100 text-[11px]">
                            {Object.keys(row.details).length} fields
                          </summary>
                          <pre className="mt-1 text-[10px] bg-zinc-50 dark:bg-zinc-950 p-2 rounded overflow-x-auto whitespace-pre-wrap break-words max-w-md">
                            {JSON.stringify(row.details, null, 2)}
                          </pre>
                        </details>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {totalPages > 1 ? (
            <div className="flex justify-end gap-2 text-xs">
              {pageNum > 1 ? (
                <Link
                  href={urlFor({ page: String(pageNum - 1) })}
                  className="px-3 py-1 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                >
                  ← Previous
                </Link>
              ) : null}
              {pageNum < totalPages ? (
                <Link
                  href={urlFor({ page: String(pageNum + 1) })}
                  className="px-3 py-1 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                >
                  Next →
                </Link>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
