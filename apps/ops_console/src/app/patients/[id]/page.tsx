import Link from "next/link";
import { notFound } from "next/navigation";

import {
  orchestrator,
  type AdherenceEventEntry,
  type Caregiver,
  type CarePlan,
  type CarePlanExemption,
  type CohortTag,
  type LanguageOption,
  type PatientCohortTagAssignment,
  type PatientDetail,
} from "@/lib/backend";

import {
  addLabFollowupAction,
  addRegimenAction,
  deactivateRegimenAction,
  fireTestDoseAction,
  fireTestLabReminderAction,
  fireTestRefillAction,
  markLabCompletedAction,
  markLabReviewedAction,
  pauseBotAction,
  resetOnboardingAction,
  unpauseBotAction,
} from "../_actions";
import {
  addCaregiverAction,
  sendCaregiverConsentPromptAction,
  sendDoctorReplyAction,
  updatePatientLanguageAction,
  assignCohortTagAction,
  confirmCaregiverConsentAction,
  createExemptionAction,
  deactivateCaregiverAction,
  removeCohortTagAction,
  revokeCaregiverConsentAction,
  revokeExemptionAction,
  setCaregiverNotifyAction,
} from "./_actions";
import { ErasePatientButton } from "./_components/ErasePatientButton";
import { ExportPatientButton } from "./_components/ExportPatientButton";
import { PatientClinicalAlerts } from "./_components/PatientClinicalAlerts";
import { PatientGoalsSection } from "./_components/PatientGoalsSection";
import { PatientTimeline } from "./_components/PatientTimeline";
import { VisitBriefSection } from "./_components/VisitBriefSection";

export const dynamic = "force-dynamic";

const ADHERENCE_BADGE: Record<AdherenceEventEntry["status"], string> = {
  taken: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
  scheduled: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  missed: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
  skipped:
    "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  delayed: "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200",
};

function formatDateTime(iso: string, timezone: string = "Asia/Kolkata") {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: timezone,
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function formatDate(iso: string, timezone: string = "Asia/Kolkata") {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: timezone,
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

export default async function PatientDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const patientId = Number(id);
  if (!patientId || Number.isNaN(patientId)) notFound();

  let patient: PatientDetail | null = null;
  let exemptions: CarePlanExemption[] = [];
  let activeCarePlans: CarePlan[] = [];
  let patientTagAssignments: PatientCohortTagAssignment[] = [];
  let activeCohortTags: CohortTag[] = [];
  let caregivers: Caregiver[] = [];
  let languages: LanguageOption[] = [];
  let error: string | null = null;
  try {
    [
      patient,
      exemptions,
      activeCarePlans,
      patientTagAssignments,
      activeCohortTags,
      caregivers,
      languages,
    ] = await Promise.all([
      orchestrator.getPatient(patientId),
      orchestrator.listPatientExemptions(patientId, { include_inactive: true }),
      orchestrator.listCarePlans(),
      orchestrator.listPatientCohortTags(patientId),
      orchestrator.listCohortTags(),
      orchestrator.listCaregivers(patientId, { include_inactive: false }),
      orchestrator.listSupportedLanguages(),
    ]);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error && /404/.test(error)) notFound();

  if (!patient) {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
        Couldn&apos;t load patient {patientId}:{" "}
        <code className="font-mono text-xs">{error ?? "unknown"}</code>
      </div>
    );
  }

  const sortedAdherence = [...patient.recent_adherence_events].sort(
    (a, b) =>
      new Date(b.scheduled_at).getTime() - new Date(a.scheduled_at).getTime(),
  );
  const adhRatePct = Math.round(
    (patient.adherence_summary.adherence_rate ?? 0) * 100,
  );

  return (
    <div className="space-y-8">
      <div>
        <Link
          href="/patients"
          className="text-xs text-zinc-500 hover:underline"
        >
          ← back to patients
        </Link>
        {/* Erasure banner — sits above the header so it's the
            first thing a doctor / ops sees when loading the
            page. The patient row PII has been overwritten; any
            "live patient" affordances below are inert. */}
        {patient.erased_at ? (
          <div className="mt-2 rounded-lg border border-zinc-400 dark:border-zinc-600 bg-zinc-100 dark:bg-zinc-800/50 p-3 text-sm">
            <div className="font-semibold text-zinc-700 dark:text-zinc-200">
              🗑 This patient&apos;s data has been erased
            </div>
            <div className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
              The patient&apos;s PII was anonymized on{" "}
              {formatDate(patient.erased_at)}. Clinical history is
              retained for medical retention but is no longer tied
              to a real person. No further outbound will be sent.
            </div>
          </div>
        ) : null}
        <div className="mt-1 flex items-center gap-2 flex-wrap">
          <h1 className="text-xl font-semibold">{patient.full_name}</h1>
          {patient.onboarding_step &&
          patient.onboarding_step !== "done" ? (
            <span className="px-2 py-0.5 text-[11px] rounded bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200 font-medium">
              onboarding: {patient.onboarding_step.replace(/_/g, " ")}
            </span>
          ) : null}
          <span className="text-[11px] text-zinc-500">
            {patient.consent_sms ? "SMS opted-in" : "SMS opted-out"}
          </span>
          {/* Bot-pause indicator. Distinct from opt-out: when set, ops
              has muted outbound for this patient regardless of consent
              state. The chip is intentionally loud (red) — a paused
              patient is in an active operational state and shouldn't
              be missed when a doctor scans the patient list. */}
          {patient.bot_paused_at ? (
            <span
              className="px-1.5 py-0.5 text-[11px] rounded bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-200 font-medium"
              title={
                patient.bot_paused_reason
                  ? `paused by ${patient.bot_paused_by ?? "ops"}: ${patient.bot_paused_reason}`
                  : "bot is paused for this patient"
              }
            >
              ⏸ bot paused
            </span>
          ) : null}
        </div>
        <div className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          <span className="font-mono">{patient.phone}</span> · id={patient.id} ·
          since {formatDate(patient.created_at)}
        </div>
        <div className="mt-2 flex items-center gap-3 flex-wrap">
          {patient.onboarding_step === "done" ? (
            <form action={resetOnboardingAction} className="inline-block">
              <input type="hidden" name="patient_id" value={patient.id} />
              <button
                type="submit"
                className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                title="Re-run the onboarding state machine on next inbound message"
              >
                Reset onboarding
              </button>
            </form>
          ) : null}
          {/* Preferred-language selector. Save fires the server action
              which validates against the orchestrator allowlist. The
              dropdown auto-submits on change so there's no extra
              "Save" button — same UX as a settings toggle. */}
          <form
            action={updatePatientLanguageAction}
            className="inline-flex items-center gap-2 text-xs"
          >
            <input type="hidden" name="patient_id" value={patient.id} />
            <label
              htmlFor={`pref-lang-${patient.id}`}
              className="text-zinc-500"
            >
              Language:
            </label>
            <select
              id={`pref-lang-${patient.id}`}
              name="preferred_language"
              defaultValue={patient.preferred_language ?? "en"}
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-xs"
            >
              {languages.map((opt) => (
                <option key={opt.code} value={opt.code}>
                  {opt.label}
                </option>
              ))}
            </select>
            <button
              type="submit"
              className="px-2 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
            >
              Save
            </button>
          </form>
          {/* Ops-initiated bot pause. Distinct from opt-out — the
              patient sees nothing, consent_sms stays true, but every
              proactive outbound is short-circuited at the dispatcher
              until ops clicks Resume. Use case: investigate a
              complaint or a concerning LLM reply before the bot
              fires again. */}
          {/* DSAR right-of-access export — sits next to the
              admin actions because exporting patient data is an
              ops/compliance operation, not a clinical one. The
              button itself is a client component so it can trigger
              a Blob-based browser download. */}
          <ExportPatientButton
            patientId={patient.id}
            patientFullName={patient.full_name}
          />
          {/* Right-of-erasure trigger. Hidden on already-erased
              patients (button is destructive + the operation is
              idempotent but rendering it twice would imply two
              separate erasures happened). */}
          {!patient.erased_at ? (
            <ErasePatientButton
              patientId={patient.id}
              patientFullName={patient.full_name}
            />
          ) : null}
          {patient.bot_paused_at ? (
            <form
              action={unpauseBotAction}
              className="inline-flex items-center gap-2 text-xs"
            >
              <input type="hidden" name="patient_id" value={patient.id} />
              <button
                type="submit"
                className="px-2 py-1 text-xs rounded border border-emerald-400 dark:border-emerald-600 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-950/40"
                title="Resume proactive outbound. Patient is not notified."
              >
                ▶ Resume bot
              </button>
            </form>
          ) : (
            <details className="inline-block">
              <summary className="cursor-pointer px-2 py-1 text-xs rounded border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40 list-none">
                ⏸ Pause bot
              </summary>
              <form
                action={pauseBotAction}
                className="mt-2 inline-flex items-center gap-2 text-xs"
              >
                <input
                  type="hidden"
                  name="patient_id"
                  value={patient.id}
                />
                <input
                  type="hidden"
                  name="actor"
                  defaultValue="ops"
                />
                <input
                  type="text"
                  name="reason"
                  required
                  placeholder="Reason (required)"
                  className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 text-xs w-56"
                />
                <button
                  type="submit"
                  className="px-2 py-1 text-xs rounded bg-red-600 hover:bg-red-700 text-white"
                  title="Halt proactive outbound until manually resumed. Patient is not notified."
                >
                  Confirm pause
                </button>
              </form>
            </details>
          )}
        </div>
      </div>

      {/* Doctor-authored reply — freeform send, in-CSW only. The
          orchestrator endpoint enforces the CSW gate; we just collapse
          the form here so it stays out of the way until needed. */}
      <details className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3">
        <summary className="text-sm font-medium cursor-pointer text-zinc-700 dark:text-zinc-200">
          Reply to patient (freeform)
        </summary>
        <form className="mt-3 space-y-2">
          <input type="hidden" name="patient_id" value={patient.id} />
          <input type="hidden" name="sent_by" defaultValue="ops" />
          <textarea
            name="body"
            rows={3}
            required
            placeholder="Type a clinical reply. Sent freeform — only works inside the 24h customer-service window."
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
          />
          <div className="flex justify-end">
            <button
              type="submit"
              formAction={sendDoctorReplyAction}
              className="px-3 py-1.5 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white text-sm"
            >
              Send reply
            </button>
          </div>
        </form>
      </details>

      {/* Side-effect / adverse-reaction reports. High-priority
          patient-safety signal — a doctor scanning this page should
          see "this patient has reported X side effects" without
          having to drill into the ops queue. Sorted newest first;
          empty state when the patient has never reported one. */}
      {patient.recent_side_effect_reports.length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-red-700 dark:text-red-300 uppercase tracking-wide flex items-center gap-2">
            <span>⚠️ Side-effect reports</span>
            <span className="text-zinc-400 normal-case font-normal">
              ({patient.recent_side_effect_reports.length})
            </span>
          </h2>
          <div className="mt-3 space-y-2">
            {patient.recent_side_effect_reports.map((report) => {
              const tone =
                report.status === "resolved"
                  ? "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900"
                  : "border-red-200 dark:border-red-900/50 bg-red-50/50 dark:bg-red-950/20";
              return (
                <div
                  key={report.ticket_id}
                  className={`rounded-lg border ${tone} p-3 text-sm`}
                >
                  <div className="flex items-baseline justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="text-zinc-500">
                        {formatDate(report.created_at)}
                      </span>
                      <span
                        className={
                          "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                          (report.status === "open"
                            ? "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200"
                            : report.status === "acknowledged"
                              ? "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
                              : "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200")
                        }
                      >
                        {report.status}
                      </span>
                      {report.sla_breached_at ? (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-red-200 text-red-900 dark:bg-red-900/60 dark:text-red-100">
                          SLA breached
                        </span>
                      ) : null}
                    </div>
                    <Link
                      href={`/tickets/${report.ticket_id}`}
                      className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                    >
                      ticket #{report.ticket_id} →
                    </Link>
                  </div>
                  {report.reported_text ? (
                    <blockquote className="mt-2 pl-3 border-l-2 border-red-300 dark:border-red-800 text-zinc-700 dark:text-zinc-300 italic whitespace-pre-line">
                      {report.reported_text}
                    </blockquote>
                  ) : (
                    <div className="mt-2 text-xs text-zinc-400 italic">
                      (no patient-said block in ticket notes — see
                      ticket for details)
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      {/* Clinical alerts — patient-safety signals raised by the
          slice-10 triage classifier. Renders nothing when the
          patient has none; floats above the visit brief when
          alerts exist so doctors see them first. */}
      <PatientClinicalAlerts patientId={patient.id} />

      {/* Care plan goals — quantitative targets the doctor
          tracks per-patient (HbA1c < 7, BP < 130/80, weight,
          etc.) with a recent-observations trend per goal.
          Renders the empty-state when no goals are set so
          the doctor can add the first one. */}
      <PatientGoalsSection patientId={patient.id} />

      {/* Visit brief — LLM-compiled 30-second summary above the
          timeline so doctors get the synthesised view first, with
          the raw events one section down for cross-checking. */}
      <VisitBriefSection patientId={patient.id} />

      {/* Unified timeline — aggregates inbound/outbound messages,
          adherence events, side-effect tickets, appointments, and
          lab events for the last 30 days. Doctor-facing
          at-a-glance: "what's been going on with this patient?". */}
      <PatientTimeline patientId={patient.id} />

      {/* Refill summary tiles — derived from already-loaded regimens, so
          no extra round-trip. Skipped entirely when no regimens have supply
          tracking enabled. */}
      {(() => {
        const tracked = patient.regimens
          .filter(
            (r) =>
              r.days_of_supply_remaining !== null &&
              (r.ends_on === null || new Date(r.ends_on) >= new Date()),
          );
        if (tracked.length === 0) return null;
        const sorted = [...tracked].sort(
          (a, b) =>
            (a.days_of_supply_remaining ?? Infinity) -
            (b.days_of_supply_remaining ?? Infinity),
        );
        const earliest = sorted[0];
        const earliestDays = earliest.days_of_supply_remaining ?? 0;
        const lowCount = tracked.filter(
          (r) => (r.days_of_supply_remaining ?? Infinity) <= 7,
        ).length;
        const earliestTone =
          earliestDays <= 3
            ? "text-red-600 dark:text-red-400"
            : earliestDays <= 7
              ? "text-amber-600 dark:text-amber-400"
              : "";
        return (
          <section>
            <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
              Refills
            </h2>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
                <div className={`text-2xl font-semibold ${earliestTone}`}>
                  {earliestDays}d
                </div>
                <div className="text-xs text-zinc-500 mt-1">
                  next refill ({earliest.medication_name})
                </div>
              </div>
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
                <div
                  className={`text-2xl font-semibold ${
                    lowCount > 0 ? "text-amber-600 dark:text-amber-400" : ""
                  }`}
                >
                  {lowCount}
                </div>
                <div className="text-xs text-zinc-500 mt-1">
                  regimens running low (≤7d)
                </div>
              </div>
            </div>
          </section>
        );
      })()}

      {/* Adherence summary tiles */}
      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-2xl font-semibold">{adhRatePct}%</div>
          <div className="text-xs text-zinc-500 mt-1">
            adherence (last {patient.adherence_summary.window_days}d)
          </div>
        </div>
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-2xl font-semibold">
            {patient.adherence_summary.taken}
          </div>
          <div className="text-xs text-zinc-500 mt-1">
            doses taken
            {patient.adherence_summary.taken > 0 && (
              <>
                <span className="block mt-0.5">
                  <span className="text-emerald-600 dark:text-emerald-400 font-medium">
                    {patient.adherence_summary.taken_on_time} on time
                  </span>
                  {" · "}
                  <span className="text-amber-600 dark:text-amber-400 font-medium">
                    {patient.adherence_summary.taken_late} late
                  </span>
                </span>
              </>
            )}
          </div>
        </div>
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-2xl font-semibold text-red-600 dark:text-red-400">
            {patient.adherence_summary.missed}
          </div>
          <div className="text-xs text-zinc-500 mt-1">missed</div>
        </div>
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
          <div className="text-2xl font-semibold">
            {patient.adherence_summary.scheduled}
          </div>
          <div className="text-xs text-zinc-500 mt-1">upcoming doses</div>
        </div>
      </section>

      {/* Regimens */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Regimens
        </h2>
        {patient.regimens.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            No regimens yet — add one below.
          </div>
        ) : (
          <div className="mt-3 space-y-3">
            {patient.regimens.map((r) => {
              const inactive =
                r.ends_on !== null && new Date(r.ends_on) < new Date();
              const times = (r.schedule.times ?? []).join(", ");
              const daysLeft = r.days_of_supply_remaining;
              const supplyTone =
                daysLeft === null
                  ? null
                  : daysLeft <= 3
                    ? "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200"
                    : daysLeft <= 7
                      ? "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
                      : "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200";
              return (
                <div
                  key={r.id}
                  className={`rounded-lg border p-4 ${
                    inactive
                      ? "border-zinc-200 dark:border-zinc-800 bg-zinc-50/50 dark:bg-zinc-900/40 opacity-70"
                      : "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900"
                  }`}
                >
                  <div className="flex items-start justify-between gap-4 flex-wrap">
                    <div>
                      <div className="font-medium flex items-center gap-2 flex-wrap">
                        <span>
                          {r.medication_name}{" "}
                          <span className="text-zinc-400">({r.dose})</span>
                        </span>
                        {daysLeft !== null && supplyTone ? (
                          <span
                            className={`px-1.5 py-0.5 text-[10px] rounded font-normal ${supplyTone}`}
                            title={`Supply started ${r.supply_started_on ?? "?"} · ${r.supply_days_initial} days initial`}
                          >
                            {daysLeft}d supply left
                          </span>
                        ) : null}
                        {inactive ? (
                          <span className="px-1.5 py-0.5 text-[10px] rounded bg-zinc-200 dark:bg-zinc-700 text-zinc-600 dark:text-zinc-300">
                            ended {formatDate(r.ends_on!)}
                          </span>
                        ) : null}
                      </div>
                      <div className="mt-1 text-xs text-zinc-500">
                        {times ? `at ${times}` : "no schedule"} ·{" "}
                        {r.schedule.timezone ?? "UTC"}
                        {r.schedule.frequency
                          ? ` · ${r.schedule.frequency}`
                          : null}
                      </div>
                    </div>
                    {!inactive ? (
                      <div className="flex items-center gap-2 flex-wrap">
                        <form action={fireTestDoseAction} className="inline">
                          <input
                            type="hidden"
                            name="patient_id"
                            value={patient.id}
                          />
                          <input
                            type="hidden"
                            name="regimen_id"
                            value={r.id}
                          />
                          <button
                            type="submit"
                            className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                            title="Fires a dose reminder NOW for demo purposes"
                          >
                            Fire test dose
                          </button>
                        </form>
                        {daysLeft !== null ? (
                          <form
                            action={fireTestRefillAction}
                            className="inline"
                          >
                            <input
                              type="hidden"
                              name="patient_id"
                              value={patient.id}
                            />
                            <input
                              type="hidden"
                              name="regimen_id"
                              value={r.id}
                            />
                            <button
                              type="submit"
                              className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                              title="Fires a refill reminder NOW for demo purposes"
                            >
                              Fire test refill
                            </button>
                          </form>
                        ) : null}
                        <form
                          action={deactivateRegimenAction}
                          className="inline"
                        >
                          <input
                            type="hidden"
                            name="patient_id"
                            value={patient.id}
                          />
                          <input
                            type="hidden"
                            name="regimen_id"
                            value={r.id}
                          />
                          <button
                            type="submit"
                            className="px-3 py-1 text-xs rounded border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/40"
                          >
                            Deactivate
                          </button>
                        </form>
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Add regimen form */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Add regimen
        </h2>
        <form
          action={addRegimenAction}
          className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 items-end"
        >
          <input type="hidden" name="patient_id" value={patient.id} />
          <label className="flex flex-col gap-1 text-xs">
            Medication
            <input
              name="medication_name"
              required
              placeholder="Metformin"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Dose
            <input
              name="dose"
              required
              placeholder="500 mg"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs sm:col-span-1 lg:col-span-2">
            Times (HH:MM, comma-separated)
            <input
              name="times"
              required
              placeholder="08:00, 20:00"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Timezone
            <input
              name="timezone"
              defaultValue="Asia/Kolkata"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Supply (days, optional)
            <input
              name="supply_days_initial"
              type="number"
              min={1}
              max={365}
              placeholder="30"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Supply started on (optional)
            <input
              name="supply_started_on"
              type="date"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <button
            type="submit"
            className="px-4 py-2 rounded bg-zinc-900 text-white hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900 sm:col-span-2 lg:col-span-1"
          >
            Add regimen
          </button>
        </form>
        <p className="mt-2 text-[11px] text-zinc-500">
          Set <span className="font-mono">Supply (days)</span> to enable refill
          reminders — patient gets a tappable reminder at T-7d, T-3d, T-1d and
          on the day supply runs out. Defaults the start date to today when
          omitted.
        </p>
      </section>

      {/* Recent adherence timeline */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Recent adherence
        </h2>
        {sortedAdherence.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            No adherence events yet.
          </div>
        ) : (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-900 text-xs uppercase tracking-wide text-zinc-500">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">When</th>
                  <th className="text-left px-4 py-2 font-medium">Medication</th>
                  <th className="text-left px-4 py-2 font-medium">Status</th>
                  <th className="text-left px-4 py-2 font-medium">Confirmed</th>
                </tr>
              </thead>
              <tbody>
                {sortedAdherence.slice(0, 30).map((e) => (
                  <tr
                    key={e.id}
                    className="border-t border-zinc-200 dark:border-zinc-800"
                  >
                    <td className="px-4 py-2">
                      {formatDateTime(e.scheduled_at)}
                    </td>
                    <td className="px-4 py-2">
                      {e.medication_name ?? (
                        <span className="text-zinc-400">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <span
                        className={`px-2 py-0.5 rounded text-xs font-medium ${ADHERENCE_BADGE[e.status]}`}
                      >
                        {e.status}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-zinc-500">
                      {e.confirmed_at ? formatDateTime(e.confirmed_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Refill timeline */}
      {patient.recent_refill_events.length > 0 ? (
        <section>
          <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
            Recent refill reminders
          </h2>
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 dark:bg-zinc-900 text-xs uppercase tracking-wide text-zinc-500">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">Scheduled</th>
                  <th className="text-left px-4 py-2 font-medium">Medication</th>
                  <th className="text-left px-4 py-2 font-medium">Stage</th>
                  <th className="text-left px-4 py-2 font-medium">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {patient.recent_refill_events.slice(0, 30).map((e) => {
                  const tone =
                    e.label === "patient refilled"
                      ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
                      : e.label === "reminder sent"
                        ? "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200"
                        : e.label === "snoozed"
                          ? "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
                          : e.label === "scheduled"
                            ? "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                            : "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200";
                  return (
                    <tr
                      key={e.id}
                      className="border-t border-zinc-200 dark:border-zinc-800"
                    >
                      <td className="px-4 py-2">
                        {formatDateTime(e.scheduled_for)}
                      </td>
                      <td className="px-4 py-2">
                        {e.medication_name ?? (
                          <span className="text-zinc-400">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-zinc-500 font-mono text-xs">
                        {e.stage ?? "—"}
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`px-2 py-0.5 rounded text-xs font-medium ${tone}`}
                        >
                          {e.label}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {/* Upcoming appointments */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Upcoming appointments
        </h2>
        {patient.upcoming_appointments.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            None scheduled.
          </div>
        ) : (
          <div className="mt-3 space-y-2">
            {patient.upcoming_appointments.map((a) => (
              <div
                key={a.id}
                className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 flex items-center justify-between gap-4 flex-wrap"
              >
                <div>
                  <div className="font-medium">
                    {a.doctor_name ?? `Doctor #${a.doctor_id}`}
                  </div>
                  <div className="text-xs text-zinc-500">
                    {formatDateTime(a.scheduled_for)} · status: {a.status}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-xs">
                  <Link
                    href={`/appointments/${a.id}`}
                    className="text-blue-600 hover:underline dark:text-blue-400"
                  >
                    Pre-visit →
                  </Link>
                  <Link
                    href={`/appointments/${a.id}/recap`}
                    className="text-blue-600 hover:underline dark:text-blue-400"
                  >
                    Recap →
                  </Link>
                  {a.calendar_html_link ? (
                    <a
                      href={a.calendar_html_link}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-600 hover:underline dark:text-blue-400"
                    >
                      Calendar event ↗
                    </a>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Caregivers */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Caregivers
        </h2>
        <p className="mt-1 text-xs text-zinc-500 max-w-2xl">
          Family members or care contacts who can receive copies of
          patient communications. A caregiver only gets messages once
          consent is confirmed AND the relevant per-channel toggle is
          on. Recap fan-out is the only channel wired today.
        </p>
        <div className="mt-3 space-y-3">
          {caregivers.length === 0 ? (
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4 text-center text-xs text-zinc-500">
              No caregivers added.
            </div>
          ) : (
            <ul className="space-y-2">
              {caregivers.map((cg) => {
                const consentBadge =
                  cg.consent_status === "confirmed"
                    ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
                    : cg.consent_status === "declined" ||
                        cg.consent_status === "revoked"
                      ? "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200"
                      : "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200";
                return (
                  <li
                    key={cg.id}
                    className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 flex items-start justify-between gap-4 flex-wrap"
                  >
                    <div className="space-y-1 text-sm flex-1 min-w-[220px]">
                      <div className="font-medium">
                        {cg.full_name}
                        {cg.relationship_to_patient ? (
                          <span className="ml-2 text-xs text-zinc-500">
                            ({cg.relationship_to_patient})
                          </span>
                        ) : null}
                        <span
                          className={
                            "ml-2 inline-block px-2 py-0.5 rounded text-[10px] font-medium uppercase " +
                            consentBadge
                          }
                        >
                          {cg.consent_status}
                        </span>
                      </div>
                      <div className="text-xs text-zinc-500 font-mono">
                        {cg.phone}
                      </div>
                      {cg.consent_confirmed_at ? (
                        <div className="text-[11px] text-zinc-500">
                          Consent {formatDateTime(cg.consent_confirmed_at)}
                          {cg.consent_confirmed_by
                            ? ` by ${cg.consent_confirmed_by}`
                            : ""}
                        </div>
                      ) : null}
                      <div className="text-[11px] text-zinc-500">
                        Recap cc:{" "}
                        {cg.notify_on_recap ? (
                          <span className="text-emerald-700 dark:text-emerald-300">
                            on
                          </span>
                        ) : (
                          <span className="text-zinc-400">off</span>
                        )}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {cg.consent_status === "pending" ? (
                        <>
                          <form>
                            <input
                              type="hidden"
                              name="patient_id"
                              value={patientId}
                            />
                            <input
                              type="hidden"
                              name="caregiver_id"
                              value={cg.id}
                            />
                            <button
                              type="submit"
                              formAction={sendCaregiverConsentPromptAction}
                              className="px-2 py-1 rounded-md border border-blue-300 dark:border-blue-800 text-xs text-blue-700 dark:text-blue-300 hover:bg-blue-50 dark:hover:bg-blue-950/40"
                              title="Send WhatsApp consent template with Yes/No buttons"
                            >
                              Send WhatsApp prompt
                            </button>
                          </form>
                          <form>
                            <input
                              type="hidden"
                              name="patient_id"
                              value={patientId}
                            />
                            <input
                              type="hidden"
                              name="caregiver_id"
                              value={cg.id}
                            />
                            <input
                              type="hidden"
                              name="confirmed_by"
                              defaultValue="ops"
                            />
                            <button
                              type="submit"
                              formAction={confirmCaregiverConsentAction}
                              className="px-2 py-1 rounded-md border border-emerald-300 dark:border-emerald-800 text-xs text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-950/40"
                              title="Record verbal consent from this caregiver (operator-attested)"
                            >
                              Confirm verbally
                            </button>
                          </form>
                        </>
                      ) : null}
                      {cg.consent_status === "confirmed" ? (
                        <form>
                          <input
                            type="hidden"
                            name="patient_id"
                            value={patientId}
                          />
                          <input
                            type="hidden"
                            name="caregiver_id"
                            value={cg.id}
                          />
                          <input
                            type="hidden"
                            name="enable"
                            value={cg.notify_on_recap ? "false" : "true"}
                          />
                          <button
                            type="submit"
                            formAction={setCaregiverNotifyAction}
                            className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 text-xs hover:bg-zinc-50 dark:hover:bg-zinc-800"
                          >
                            {cg.notify_on_recap
                              ? "Disable recap cc"
                              : "Enable recap cc"}
                          </button>
                        </form>
                      ) : null}
                      {cg.consent_status === "confirmed" ? (
                        <form>
                          <input
                            type="hidden"
                            name="patient_id"
                            value={patientId}
                          />
                          <input
                            type="hidden"
                            name="caregiver_id"
                            value={cg.id}
                          />
                          <button
                            type="submit"
                            formAction={revokeCaregiverConsentAction}
                            className="px-2 py-1 rounded-md border border-amber-300 dark:border-amber-800 text-xs text-amber-700 dark:text-amber-300 hover:bg-amber-50 dark:hover:bg-amber-950/40"
                          >
                            Revoke consent
                          </button>
                        </form>
                      ) : null}
                      <form>
                        <input
                          type="hidden"
                          name="patient_id"
                          value={patientId}
                        />
                        <input
                          type="hidden"
                          name="caregiver_id"
                          value={cg.id}
                        />
                        <button
                          type="submit"
                          formAction={deactivateCaregiverAction}
                          className="px-2 py-1 rounded-md border border-red-300 dark:border-red-800 text-xs text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40"
                        >
                          Remove
                        </button>
                      </form>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}

          <form className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
            <input type="hidden" name="patient_id" value={patientId} />
            <label className="text-xs text-zinc-500 flex flex-col gap-1">
              Full name
              <input
                type="text"
                name="full_name"
                required
                placeholder="e.g. Anita Karnatak"
                className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
              />
            </label>
            <label className="text-xs text-zinc-500 flex flex-col gap-1">
              Phone
              <input
                type="tel"
                name="phone"
                required
                placeholder="918340858xxx"
                className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm font-mono"
              />
            </label>
            <label className="text-xs text-zinc-500 flex flex-col gap-1">
              Relationship
              <input
                type="text"
                name="relationship_to_patient"
                placeholder="spouse / daughter / son…"
                className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
              />
            </label>
            <label className="text-xs text-zinc-500 flex items-center gap-2 self-end pb-2">
              <input
                type="checkbox"
                name="notify_on_recap"
                defaultChecked
              />
              cc on recaps
            </label>
            <button
              type="submit"
              formAction={addCaregiverAction}
              className="md:col-span-1 justify-self-start px-3 py-2 rounded-md bg-zinc-900 hover:bg-zinc-700 text-white text-sm dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
            >
              Add caregiver
            </button>
          </form>
        </div>
      </section>

      {/* Cohort tags */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Cohort tags
        </h2>
        <p className="mt-1 text-xs text-zinc-500 max-w-2xl">
          Clinician-authored cohort labels. Patients with a tag are
          included in the sweep for any care plan targeting that tag.
        </p>
        {(() => {
          const assignedTagIds = new Set(
            patientTagAssignments.map((a) => a.cohort_tag_id),
          );
          const eligibleTags = activeCohortTags.filter(
            (t) => !assignedTagIds.has(t.id),
          );
          return (
            <div className="mt-3 space-y-3">
              {patientTagAssignments.length === 0 ? (
                <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4 text-center text-xs text-zinc-500">
                  No cohort tags assigned.
                </div>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {patientTagAssignments.map((a) => (
                    <li
                      key={a.id}
                      className="inline-flex items-center gap-2 rounded-full bg-indigo-100 dark:bg-indigo-950/40 text-indigo-800 dark:text-indigo-200 pl-3 pr-1 py-1 text-xs"
                    >
                      <span className="font-medium">{a.cohort_tag_label}</span>
                      <span className="text-[10px] opacity-70">
                        {a.assigned_by ? `· ${a.assigned_by}` : ""}
                        {" · "}
                        {formatDateTime(a.assigned_at)}
                      </span>
                      <form>
                        <input
                          type="hidden"
                          name="patient_id"
                          value={patientId}
                        />
                        <input
                          type="hidden"
                          name="cohort_tag_id"
                          value={a.cohort_tag_id}
                        />
                        <button
                          type="submit"
                          formAction={removeCohortTagAction}
                          aria-label={`Remove ${a.cohort_tag_label}`}
                          className="rounded-full px-2 py-0.5 text-indigo-700 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-900"
                        >
                          ×
                        </button>
                      </form>
                    </li>
                  ))}
                </ul>
              )}

              {eligibleTags.length > 0 ? (
                <form className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
                  <input
                    type="hidden"
                    name="patient_id"
                    value={patientId}
                  />
                  <input
                    type="hidden"
                    name="assigned_by"
                    defaultValue="ops"
                  />
                  <label className="text-xs text-zinc-500 flex flex-col gap-1 md:col-span-2">
                    Add tag
                    <select
                      name="cohort_tag_id"
                      required
                      defaultValue=""
                      className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
                    >
                      <option value="" disabled>
                        Select tag…
                      </option>
                      {eligibleTags.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.label}
                          {t.description ? ` — ${t.description}` : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="submit"
                    formAction={assignCohortTagAction}
                    className="px-3 py-2 rounded-md bg-zinc-900 hover:bg-zinc-700 text-white text-sm dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
                  >
                    Assign
                  </button>
                </form>
              ) : activeCohortTags.length > 0 ? (
                <div className="text-xs text-zinc-500">
                  Already assigned to every active cohort tag.
                </div>
              ) : (
                <div className="text-xs text-zinc-500">
                  No cohort tags configured yet —{" "}
                  <Link
                    href="/cohort-tags"
                    className="text-blue-600 hover:underline dark:text-blue-400"
                  >
                    create one
                  </Link>
                  .
                </div>
              )}
            </div>
          );
        })()}
      </section>

      {/* Care-plan exemptions */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Care plan exemptions
        </h2>
        <p className="mt-1 text-xs text-zinc-500 max-w-2xl">
          Skip a standing-order care plan for this patient — useful for
          contraindications, alternate care under a specialist, or
          patient-specific schedules. Revoke when the reason no longer
          applies.
        </p>
        {(() => {
          const activeExemptions = exemptions.filter((e) => e.is_active);
          const historicalExemptions = exemptions.filter((e) => !e.is_active);
          const exemptedPlanIds = new Set(
            activeExemptions.map((e) => e.care_plan_id),
          );
          const eligiblePlans = activeCarePlans.filter(
            (p) => !exemptedPlanIds.has(p.id),
          );
          return (
            <div className="mt-3 space-y-3">
              {activeExemptions.length === 0 ? (
                <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4 text-center text-xs text-zinc-500">
                  No active exemptions for this patient.
                </div>
              ) : (
                <ul className="space-y-2">
                  {activeExemptions.map((ex) => (
                    <li
                      key={ex.id}
                      className="rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-3 flex items-start justify-between gap-4"
                    >
                      <div className="space-y-1 text-sm">
                        <div className="font-medium">
                          {ex.care_plan_test_name ?? `Plan #${ex.care_plan_id}`}
                          {ex.care_plan_cohort ? (
                            <span className="ml-2 text-xs text-zinc-500">
                              ({ex.care_plan_cohort})
                            </span>
                          ) : null}
                        </div>
                        <div className="text-xs text-zinc-700 dark:text-zinc-300">
                          {ex.reason}
                        </div>
                        <div className="text-[11px] text-zinc-500">
                          Created {formatDateTime(ex.created_at)}
                          {ex.created_by ? ` by ${ex.created_by}` : ""}
                          {ex.expires_at
                            ? ` · expires ${formatDateTime(ex.expires_at)}`
                            : " · no expiry"}
                        </div>
                      </div>
                      <form>
                        <input
                          type="hidden"
                          name="patient_id"
                          value={patientId}
                        />
                        <input type="hidden" name="exemption_id" value={ex.id} />
                        <input
                          type="hidden"
                          name="revoked_by"
                          defaultValue="ops"
                        />
                        <button
                          type="submit"
                          formAction={revokeExemptionAction}
                          className="px-2 py-1 rounded-md border border-red-300 dark:border-red-800 text-xs text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-950/40"
                        >
                          Revoke
                        </button>
                      </form>
                    </li>
                  ))}
                </ul>
              )}

              {/* Add new exemption */}
              {eligiblePlans.length > 0 ? (
                <form className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
                  <input
                    type="hidden"
                    name="patient_id"
                    value={patientId}
                  />
                  <label className="text-xs text-zinc-500 flex flex-col gap-1 md:col-span-2">
                    Plan
                    <select
                      name="care_plan_id"
                      required
                      defaultValue=""
                      className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
                    >
                      <option value="" disabled>
                        Select plan…
                      </option>
                      {eligiblePlans.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.test_name} ({p.cohort_attr})
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-xs text-zinc-500 flex flex-col gap-1 md:col-span-2">
                    Reason
                    <input
                      type="text"
                      name="reason"
                      required
                      placeholder="e.g. Under nephrology — alternate schedule"
                      className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
                    />
                  </label>
                  <label className="text-xs text-zinc-500 flex flex-col gap-1">
                    Expires
                    <input
                      type="date"
                      name="expires_at"
                      className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
                    />
                  </label>
                  <input
                    type="hidden"
                    name="created_by"
                    defaultValue="ops"
                  />
                  <button
                    type="submit"
                    formAction={createExemptionAction}
                    className="md:col-span-5 justify-self-start px-3 py-2 rounded-md bg-zinc-900 hover:bg-zinc-700 text-white text-sm dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
                  >
                    Add exemption
                  </button>
                </form>
              ) : activeCarePlans.length > 0 ? (
                <div className="text-xs text-zinc-500">
                  This patient is already exempted from every active care
                  plan.
                </div>
              ) : null}

              {historicalExemptions.length > 0 ? (
                <details className="text-xs">
                  <summary className="cursor-pointer text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100">
                    Past exemptions ({historicalExemptions.length})
                  </summary>
                  <ul className="mt-2 space-y-1 text-zinc-500">
                    {historicalExemptions.map((ex) => (
                      <li key={ex.id} className="font-mono">
                        #{ex.id} {ex.care_plan_test_name ?? `plan-${ex.care_plan_id}`} —{" "}
                        {ex.revoked_at ? "revoked" : "expired"}
                        {ex.revoked_at
                          ? ` ${formatDateTime(ex.revoked_at)}`
                          : ex.expires_at
                            ? ` ${formatDateTime(ex.expires_at)}`
                            : ""}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          );
        })()}
      </section>

      {/* Lab follow-ups */}
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Lab follow-ups
        </h2>
        {patient.lab_followups.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            None tracked yet — add one below.
          </div>
        ) : (
          <div className="mt-3 space-y-2">
            {patient.lab_followups.map((lab) => {
              const statusTone =
                lab.status === "reviewed"
                  ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
                  : lab.status === "completed"
                    ? "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200"
                    : lab.status === "booked"
                      ? "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
                      : lab.is_overdue
                        ? "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200"
                        : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";
              const dueDescriptor =
                lab.due_by === null
                  ? "no due date"
                  : lab.is_overdue
                    ? `overdue by ${Math.abs(lab.days_until_due ?? 0)}d`
                    : (lab.days_until_due ?? 0) === 0
                      ? "due today"
                      : `due in ${lab.days_until_due}d (${formatDate(lab.due_by)})`;
              return (
                <div
                  key={lab.id}
                  className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 flex items-start justify-between gap-4 flex-wrap"
                >
                  <div>
                    <div className="font-medium flex items-center gap-2 flex-wrap">
                      <span>{lab.test_name}</span>
                      <span
                        className={`px-1.5 py-0.5 text-[10px] rounded font-normal ${statusTone}`}
                      >
                        {lab.status}
                      </span>
                      {lab.is_overdue ? (
                        <span className="px-1.5 py-0.5 text-[10px] rounded bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200 font-normal">
                          overdue
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-1 text-xs text-zinc-500">
                      {dueDescriptor}
                      {lab.notes ? ` · ${lab.notes}` : null}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    {lab.status === "due" || lab.status === "booked" ? (
                      <form action={fireTestLabReminderAction} className="inline">
                        <input
                          type="hidden"
                          name="patient_id"
                          value={patient.id}
                        />
                        <input type="hidden" name="lab_id" value={lab.id} />
                        <button
                          type="submit"
                          className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                          title="Fires a lab reminder NOW for demo purposes"
                        >
                          Fire test reminder
                        </button>
                      </form>
                    ) : null}
                    {lab.status === "due" || lab.status === "booked" ? (
                      <form action={markLabCompletedAction} className="inline">
                        <input
                          type="hidden"
                          name="patient_id"
                          value={patient.id}
                        />
                        <input type="hidden" name="lab_id" value={lab.id} />
                        <button
                          type="submit"
                          className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                        >
                          Mark completed
                        </button>
                      </form>
                    ) : null}
                    {lab.status === "completed" ? (
                      <form action={markLabReviewedAction} className="inline">
                        <input
                          type="hidden"
                          name="patient_id"
                          value={patient.id}
                        />
                        <input type="hidden" name="lab_id" value={lab.id} />
                        <button
                          type="submit"
                          className="px-3 py-1 text-xs rounded border border-emerald-300 text-emerald-700 hover:bg-emerald-50 dark:border-emerald-800 dark:text-emerald-300 dark:hover:bg-emerald-950/40"
                        >
                          Mark reviewed
                        </button>
                      </form>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <h3 className="mt-6 text-xs font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Add lab follow-up
        </h3>
        <form
          action={addLabFollowupAction}
          className="mt-2 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 items-end"
        >
          <input type="hidden" name="patient_id" value={patient.id} />
          <label className="flex flex-col gap-1 text-xs">
            Test name
            <input
              name="test_name"
              required
              placeholder="HbA1c"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Due by
            <input
              name="due_by"
              type="date"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs sm:col-span-2 lg:col-span-1">
            Notes (optional)
            <input
              name="notes"
              placeholder="fasting required"
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <button
            type="submit"
            className="px-4 py-2 rounded bg-zinc-900 text-white hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900"
          >
            Add lab follow-up
          </button>
        </form>
      </section>
    </div>
  );
}
