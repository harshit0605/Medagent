"use client";

import { useActionState } from "react";

import { routeAction, type RouteActionState } from "../_actions";

const RUNNER_BADGE: Record<string, string> = {
  langgraph: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-200",
  sync_fallback: "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
};

const RISK_BADGE: Record<string, string> = {
  critical: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200",
  medium: "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-200",
  low: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
};

const SAMPLES: { label: string; patient: string; text: string }[] = [
  { label: "Refill (paraphrase)", patient: "demo-1", text: "hey im running low on my pills" },
  { label: "Critical symptom", patient: "demo-2", text: "I have chest pain and cannot breathe" },
  { label: "Adherence taken", patient: "demo-3", text: "took my morning dose" },
  { label: "Lab booked", patient: "demo-4", text: "Lab booked" },
  { label: "Subtle red flag", patient: "demo-5", text: "chest is tight and im gasping a bit" },
];

export function RouteTesterForm() {
  const [state, action, pending] = useActionState<RouteActionState | null, FormData>(
    routeAction,
    null,
  );

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
      <form action={action} className="space-y-4">
        <div>
          <label className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Patient ID
          </label>
          <input
            name="patient_id"
            required
            defaultValue={state?.request?.patient_id ?? ""}
            placeholder="e.g. p-test-1"
            className="mt-1 w-full px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
          />
        </div>
        <div>
          <label className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Phone (optional)
          </label>
          <input
            name="phone"
            placeholder="+10000000000"
            className="mt-1 w-full px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
          />
        </div>
        <div>
          <label className="text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Patient message
          </label>
          <textarea
            name="text"
            required
            rows={4}
            defaultValue={state?.request?.text ?? ""}
            placeholder="Type the inbound WhatsApp message..."
            className="mt-1 w-full px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 font-mono text-sm"
          />
        </div>
        <button
          type="submit"
          disabled={pending}
          className="px-4 py-2 rounded bg-zinc-900 hover:bg-zinc-800 text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {pending ? "Routing..." : "Route message"}
        </button>

        <div className="pt-2">
          <div className="text-xs text-zinc-500 dark:text-zinc-400 mb-2">Quick samples:</div>
          <div className="flex flex-wrap gap-2">
            {SAMPLES.map((sample) => (
              <button
                key={sample.label}
                type="button"
                onClick={(event) => {
                  const form = event.currentTarget.closest("form");
                  if (!form) return;
                  (form.elements.namedItem("patient_id") as HTMLInputElement).value =
                    sample.patient;
                  (form.elements.namedItem("text") as HTMLTextAreaElement).value =
                    sample.text;
                }}
                className="text-xs px-2 py-1 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
              >
                {sample.label}
              </button>
            ))}
          </div>
        </div>
      </form>

      <div className="space-y-4">
        {!state ? (
          <p className="text-sm text-zinc-500">
            Submit a message to see the agent decision.
          </p>
        ) : !state.ok ? (
          <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
            <div className="font-medium">Route failed</div>
            <div className="mt-1 font-mono text-xs">{state.error}</div>
          </div>
        ) : (
          <RouteResultCard result={state.result!} />
        )}
      </div>
    </div>
  );
}

function RouteResultCard({ result }: { result: NonNullable<RouteActionState["result"]> }) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className={
            "px-2 py-0.5 rounded text-xs font-medium " +
            (RUNNER_BADGE[result.runner] ?? "")
          }
        >
          {result.runner}
        </span>
        <span
          className={
            "px-2 py-0.5 rounded text-xs font-medium " +
            (RISK_BADGE[result.risk_level] ?? "")
          }
        >
          {result.risk_level}
        </span>
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-zinc-100 dark:bg-zinc-800">
          intent: {result.intent}
        </span>
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-zinc-100 dark:bg-zinc-800">
          flow: {result.flow_action}
        </span>
        {result.escalation_required ? (
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-600 text-white">
            ESCALATION
          </span>
        ) : null}
        {result.ticket_id ? (
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-amber-600 text-white">
            ticket #{result.ticket_id}
          </span>
        ) : null}
      </div>

      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500 mb-1">Response body</div>
        <div className="rounded border border-zinc-200 dark:border-zinc-800 p-3 text-sm whitespace-pre-wrap bg-zinc-50 dark:bg-zinc-950">
          {result.message_out.body}
        </div>
        <div className="mt-1 text-xs text-zinc-500">
          template: {result.message_out.template_name ?? "—"} · use_template:{" "}
          {String(result.message_out.use_template)} · quick replies:{" "}
          {result.message_out.quick_replies.map((r) => r.title).join(", ") || "—"}
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500 mb-1">Policy</div>
        <div className="text-sm">{result.policy.reason}</div>
        <div className="mt-1 text-xs text-zinc-500">
          freeform window: {String(result.policy.in_customer_service_window)} ·
          use_template: {String(result.policy.use_template)}
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500 mb-1">
          Audit reasons
        </div>
        <ul className="text-xs font-mono space-y-0.5">
          {result.audit_reasons.map((reason) => (
            <li key={reason}>· {reason}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}
