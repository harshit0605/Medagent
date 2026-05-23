import Link from "next/link";

import {
  orchestrator,
  type AppointmentDetail,
  type AppointmentRecap,
  type RecapMedItem,
} from "@/lib/backend";

import {
  previewRecapAction,
  saveRecapAction,
  sendRecapAction,
} from "./_actions";

export const dynamic = "force-dynamic";

const STATUS_BADGE: Record<string, string> = {
  draft: "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
  sent: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  acknowledged:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  questioned:
    "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
};

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** Each med item rendered as one line: "name -- instructions" / "name -- change" */
function medsToLines(items: RecapMedItem[] | undefined): string {
  if (!items) return "";
  return items
    .map((m) => {
      const detail = m.instructions ?? m.change ?? "";
      return detail ? `${m.name} -- ${detail}` : m.name;
    })
    .join("\n");
}

export default async function AppointmentRecapPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const appointmentId = Number(id);
  if (!Number.isFinite(appointmentId) || appointmentId <= 0) {
    return <div className="text-sm text-red-600">Invalid appointment id.</div>;
  }

  let appointment: AppointmentDetail | null = null;
  let recap: AppointmentRecap | null = null;
  let error: string | null = null;
  try {
    [appointment, recap] = await Promise.all([
      orchestrator.getAppointment(appointmentId),
      orchestrator.getAppointmentRecap(appointmentId),
    ]);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error || !appointment) {
    return (
      <div className="space-y-3">
        <Link
          href="/patients"
          className="text-sm text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          ← back
        </Link>
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load appointment.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      </div>
    );
  }

  const structured = (recap?.structured_payload ?? {}) as Record<
    string,
    unknown
  >;
  const isSent = recap?.status && recap.status !== "draft";
  const readonly = !!isSent;

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <Link
          href={`/patients/${appointment.patient_id}`}
          className="text-sm text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          ← back to patient
        </Link>
        <div className="mt-2 flex items-baseline justify-between flex-wrap gap-3">
          <h1 className="text-xl font-semibold">After-visit recap</h1>
          {recap ? (
            <span
              className={
                "px-2 py-0.5 rounded text-xs font-medium uppercase " +
                (STATUS_BADGE[recap.status] ?? STATUS_BADGE.draft)
              }
            >
              {recap.status}
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded text-xs font-medium uppercase bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
              new
            </span>
          )}
        </div>
        <div className="mt-1 text-sm text-zinc-500">
          {appointment.patient_full_name ?? `Patient #${appointment.patient_id}`}
          {" · "}
          {appointment.doctor_name ?? `Doctor #${appointment.doctor_id}`}
          {" · "}
          {formatDateTime(appointment.scheduled_for)}
        </div>
        {recap?.sent_at ? (
          <div className="mt-1 text-xs text-zinc-500">
            Sent {formatDateTime(recap.sent_at)}
            {recap.sent_message_id ? (
              <>
                {" · "}
                <span className="font-mono">{recap.sent_message_id}</span>
              </>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Form. The same form serves Save / Preview / Send via formAction. */}
      <form className="space-y-5">
        <input type="hidden" name="appointment_id" value={appointmentId} />

        <section>
          <label className="block text-sm font-medium mb-1">
            Doctor&apos;s notes
          </label>
          <textarea
            name="doctor_notes"
            rows={4}
            disabled={readonly}
            defaultValue={recap?.doctor_notes ?? ""}
            placeholder="Free-text notes the patient should see (paste from EHR or dictate). Keep it plain language."
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950 disabled:text-zinc-500"
          />
        </section>

        <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">
              Meds added
            </label>
            <p className="text-[11px] text-zinc-500 mb-1">
              One per line · format: <code>Name -- instructions</code>
            </p>
            <textarea
              name="meds_added"
              rows={4}
              disabled={readonly}
              defaultValue={medsToLines(
                structured.meds_added as RecapMedItem[] | undefined,
              )}
              placeholder={"Vitamin D3 -- 1 tab daily after breakfast"}
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950 disabled:text-zinc-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">
              Meds changed
            </label>
            <p className="text-[11px] text-zinc-500 mb-1">
              One per line · format: <code>Name -- change</code>
            </p>
            <textarea
              name="meds_changed"
              rows={4}
              disabled={readonly}
              defaultValue={medsToLines(
                structured.meds_changed as RecapMedItem[] | undefined,
              )}
              placeholder={"Metformin -- increase to 500mg twice daily"}
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950 disabled:text-zinc-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">
              Meds stopped
            </label>
            <p className="text-[11px] text-zinc-500 mb-1">
              One per line, just the name.
            </p>
            <textarea
              name="meds_stopped"
              rows={4}
              disabled={readonly}
              defaultValue={medsToLines(
                structured.meds_stopped as RecapMedItem[] | undefined,
              )}
              placeholder={"Old beta-blocker"}
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950 disabled:text-zinc-500"
            />
          </div>
        </section>

        <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">
              Tests / labs ordered
            </label>
            <p className="text-[11px] text-zinc-500 mb-1">One per line.</p>
            <textarea
              name="labs_ordered"
              rows={3}
              disabled={readonly}
              defaultValue={(
                (structured.labs_ordered ?? []) as { test_name: string }[]
              )
                .map((l) => l.test_name)
                .join("\n")}
              placeholder={"HbA1c\nFasting glucose"}
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950 disabled:text-zinc-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">
              Red-flag warnings
            </label>
            <p className="text-[11px] text-zinc-500 mb-1">
              One per line · &quot;call us if&quot; symptoms.
            </p>
            <textarea
              name="red_flags"
              rows={3}
              disabled={readonly}
              defaultValue={((structured.red_flags ?? []) as string[]).join("\n")}
              placeholder={
                "chest pain or pressure\nblood sugar below 70 mg/dL\nfainting or severe dizziness"
              }
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950 disabled:text-zinc-500"
            />
          </div>
        </section>

        <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">
              Next follow-up in
            </label>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={0}
                max={365}
                name="next_followup_in_days"
                disabled={readonly}
                defaultValue={
                  (structured.next_followup_in_days as number | null) ?? ""
                }
                className="w-24 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950"
              />
              <span className="text-sm text-zinc-500">days</span>
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">
              Authored by
            </label>
            <input
              type="text"
              name="authored_by"
              disabled={readonly}
              defaultValue={recap?.authored_by ?? ""}
              placeholder="Dr. handle / role"
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-zinc-50 disabled:dark:bg-zinc-950"
            />
          </div>
        </section>

        {!readonly ? (
          <div className="flex flex-wrap gap-2 pt-2 border-t border-zinc-200 dark:border-zinc-800">
            <button
              type="submit"
              formAction={saveRecapAction}
              className="px-3 py-1.5 rounded-md border border-zinc-300 dark:border-zinc-700 text-sm hover:bg-zinc-50 dark:hover:bg-zinc-800"
            >
              Save draft
            </button>
            <button
              type="submit"
              formAction={previewRecapAction}
              className="px-3 py-1.5 rounded-md border border-blue-500 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-200 text-sm hover:bg-blue-100"
            >
              Generate preview
            </button>
            <button
              type="submit"
              formAction={sendRecapAction}
              className="px-3 py-1.5 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white text-sm"
            >
              Send to patient
            </button>
          </div>
        ) : null}
      </form>

      {recap?.generated_text ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            {readonly ? "Sent to patient" : "Preview"}
          </h2>
          <pre className="mt-2 whitespace-pre-wrap rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-4 text-sm text-zinc-800 dark:text-zinc-200 font-sans">
            {recap.generated_text}
          </pre>
        </section>
      ) : null}
    </div>
  );
}
