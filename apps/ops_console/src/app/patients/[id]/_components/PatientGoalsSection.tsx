import {
  orchestrator,
  type CarePlanGoal,
} from "@/lib/backend";

import {
  createPatientGoalAction,
  recordGoalObservationAction,
  updatePatientGoalStatusAction,
} from "../_actions";

const STATUS_TONE: Record<CarePlanGoal["status"], string> = {
  active: "bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200",
  achieved:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200",
  inactive:
    "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};

// Drift badge styling. Order of severity (left = worse):
//   slipping      — was on-target, now regressing. Red.
//   persistent_off — never got there. Amber.
//   stale         — engagement gap (no recent data). Amber.
//   on_target / no_data — no badge at all (clean state).
const DRIFT_BADGE: Record<
  Exclude<NonNullable<CarePlanGoal["drift_status"]>, "on_target" | "no_data">,
  { label: string; tone: string; title: string }
> = {
  slipping: {
    label: "↘ slipping",
    tone: "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200",
    title:
      "Most recent observation is off-target after one or more on-target values — worth reviewing.",
  },
  persistent_off: {
    label: "× persistent off",
    tone: "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
    title:
      "All recent observations off-target — consider regimen adjustment.",
  },
  stale: {
    label: "⏳ stale",
    tone: "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
    title:
      "No recent observations — re-engage the patient.",
  },
};

// Common metric examples — shown as a placeholder block so
// the doctor knows what to type. (We could promote these to
// client-side presets that auto-fill the form; deferred to
// a future iteration to keep this component server-only.)
const METRIC_HINTS = [
  "hba1c · %",
  "bp_systolic · mmHg",
  "bp_diastolic · mmHg",
  "fasting_glucose · mg/dL",
  "weight_kg · kg",
  "ldl · mg/dL",
  "exercise_min_week · min",
];

function formatRelative(iso: string): string {
  const diff = Math.round(
    (Date.now() - new Date(iso).getTime()) / 1000,
  );
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function formatComparator(g: CarePlanGoal): string {
  const op = g.comparator === "less_than" ? "<" : ">";
  return `${op} ${g.target_value} ${g.target_unit}`;
}

export async function PatientGoalsSection({
  patientId,
}: {
  patientId: number;
}) {
  let goals: CarePlanGoal[] = [];
  let error: string | null = null;
  try {
    goals = await orchestrator.listPatientGoals(patientId);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  // Active goals are the workflow surface. Other statuses live
  // behind a disclosure so the patient detail page stays
  // focused on the live tracking view.
  const active = goals.filter((g) => g.status === "active");
  const archived = goals.filter((g) => g.status !== "active");

  return (
    <section>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Care plan goals
          {active.length > 0 ? (
            <span className="ml-2 text-zinc-400 normal-case font-normal">
              ({active.length} active)
            </span>
          ) : null}
        </h2>
      </div>

      {error ? (
        <div className="mt-3 rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-3 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load goals.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      ) : null}

      {active.length === 0 && !error ? (
        <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-4 text-center text-xs text-zinc-500">
          No active goals — add one below to track this
          patient&apos;s clinical targets over time.
        </div>
      ) : (
        <ol className="mt-3 space-y-3">
          {active.map((g) => (
            <GoalCard key={g.id} goal={g} patientId={patientId} />
          ))}
        </ol>
      )}

      {/* Add-goal form. Hidden by default to keep visual
          weight low when the doctor isn't editing — the
          common workflow is "look at active goals", not
          "set a new one". */}
      <details className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3 text-xs">
        <summary className="cursor-pointer text-zinc-700 dark:text-zinc-200 font-medium">
          + Add goal
        </summary>
        <form
          action={createPatientGoalAction}
          className="mt-3 grid grid-cols-2 gap-2"
        >
          <input
            type="hidden"
            name="patient_id"
            value={patientId}
          />
          <p className="col-span-2 text-[10px] text-zinc-500">
            Common metrics (free-form):{" "}
            {METRIC_HINTS.join(" · ")}
          </p>
          <label>
            metric key
            <input
              name="metric_key"
              required
              placeholder="hba1c"
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            />
          </label>
          <label>
            display label
            <input
              name="metric_label"
              required
              placeholder="HbA1c"
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            />
          </label>
          <label>
            comparator
            <select
              name="comparator"
              defaultValue="less_than"
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            >
              <option value="less_than">&lt; less than</option>
              <option value="greater_than">&gt; greater than</option>
            </select>
          </label>
          <label>
            target value
            <input
              name="target_value"
              required
              type="number"
              step="any"
              placeholder="7"
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            />
          </label>
          <label>
            unit
            <input
              name="target_unit"
              required
              placeholder="%"
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            />
          </label>
          <label>
            ends on (optional)
            <input
              name="ends_on"
              type="date"
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            />
          </label>
          <label className="col-span-2">
            notes (optional)
            <textarea
              name="notes"
              rows={2}
              className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
            />
          </label>
          <button
            type="submit"
            className="col-span-2 px-3 py-1 rounded bg-zinc-900 hover:bg-zinc-700 text-white dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            Add goal
          </button>
        </form>
      </details>

      {archived.length > 0 ? (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">
            {archived.length} archived (achieved / inactive)
          </summary>
          <ol className="mt-2 space-y-2">
            {archived.map((g) => (
              <GoalCard
                key={g.id}
                goal={g}
                patientId={patientId}
                compact
              />
            ))}
          </ol>
        </details>
      ) : null}
    </section>
  );
}

async function GoalCard({
  goal,
  patientId,
  compact,
}: {
  goal: CarePlanGoal;
  patientId: number;
  compact?: boolean;
}) {
  // Fetch the trend for the trail bar — last 10 observations,
  // server-side so the patient page stays static-rendered.
  // We accept the extra round-trip per goal because the
  // count is small (active goals are usually 2-5).
  let observations: Awaited<
    ReturnType<typeof orchestrator.listGoalObservations>
  > = [];
  try {
    observations = await orchestrator.listGoalObservations(
      patientId,
      goal.id,
      { limit: 10 },
    );
  } catch {
    observations = [];
  }

  // Sparkline-as-text — no charting lib. The newest values go
  // last so the trend reads left-to-right oldest → newest,
  // matching how the doctor reads "is this getting better?".
  const sparkValues = [...observations]
    .reverse()
    .map((o) => o.value);

  const onTargetTone =
    goal.on_target === null
      ? ""
      : goal.on_target
        ? "text-emerald-700 dark:text-emerald-300"
        : "text-red-700 dark:text-red-300";

  return (
    <li
      className={
        "rounded-lg border " +
        (goal.status === "active"
          ? "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900"
          : "border-zinc-200 dark:border-zinc-800 bg-zinc-50/50 dark:bg-zinc-950/40 opacity-80") +
        (compact ? " p-2 text-xs" : " p-3 text-sm")
      }
    >
      <header className="flex items-baseline justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="font-medium">{goal.metric_label}</span>
          <span className="text-xs text-zinc-500 font-mono">
            target {formatComparator(goal)}
          </span>
          <span
            className={
              "px-1.5 py-0.5 rounded text-[10px] font-medium " +
              STATUS_TONE[goal.status]
            }
          >
            {goal.status}
          </span>
          {/* Drift badge — only renders for the alert-worthy
              states (slipping / persistent_off / stale).
              on_target + no_data don't badge so the clean
              cases stay visually quiet. */}
          {goal.drift_status === "slipping" ||
          goal.drift_status === "persistent_off" ||
          goal.drift_status === "stale" ? (
            <span
              className={
                "px-1.5 py-0.5 rounded text-[10px] font-medium " +
                DRIFT_BADGE[goal.drift_status].tone
              }
              title={DRIFT_BADGE[goal.drift_status].title}
            >
              {DRIFT_BADGE[goal.drift_status].label}
            </span>
          ) : null}
        </div>
        {goal.latest_value !== null ? (
          <span className={`text-sm font-semibold ${onTargetTone}`}>
            {goal.latest_value} {goal.target_unit}
            <span className="ml-1 text-[10px] text-zinc-500">
              {goal.on_target ? "✓ on target" : "× off target"}
            </span>
          </span>
        ) : (
          <span className="text-xs text-zinc-400">(no observations yet)</span>
        )}
      </header>

      {sparkValues.length > 1 ? (
        <div className="mt-1 text-[10px] text-zinc-500 font-mono tabular-nums">
          trend: {sparkValues.join(" → ")}
        </div>
      ) : null}

      {goal.notes ? (
        <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400 italic">
          {goal.notes}
        </p>
      ) : null}

      {!compact && goal.status === "active" ? (
        <div className="mt-2 flex items-baseline justify-between gap-2 flex-wrap">
          <details className="text-xs">
            <summary className="cursor-pointer text-blue-600 dark:text-blue-400 hover:underline">
              + Record observation
            </summary>
            <form
              action={recordGoalObservationAction}
              className="mt-2 grid grid-cols-2 gap-2"
            >
              <input
                type="hidden"
                name="patient_id"
                value={patientId}
              />
              <input type="hidden" name="goal_id" value={goal.id} />
              <input
                type="hidden"
                name="unit"
                value={goal.target_unit}
              />
              <label>
                value ({goal.target_unit})
                <input
                  name="value"
                  required
                  type="number"
                  step="any"
                  className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
                />
              </label>
              <label>
                observed at
                <input
                  name="observed_at"
                  type="datetime-local"
                  className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
                />
              </label>
              <label className="col-span-2">
                notes (optional)
                <input
                  name="notes"
                  className="block w-full rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
                />
              </label>
              <button
                type="submit"
                className="col-span-2 px-2.5 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700 text-xs"
              >
                Record
              </button>
            </form>
          </details>

          <div className="flex gap-1.5">
            <form action={updatePatientGoalStatusAction}>
              <input type="hidden" name="patient_id" value={patientId} />
              <input type="hidden" name="goal_id" value={goal.id} />
              <input type="hidden" name="status" value="achieved" />
              <button
                type="submit"
                className="text-[10px] px-2 py-0.5 rounded border border-emerald-300 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-950/40"
                title="Mark this goal as achieved"
              >
                Mark achieved
              </button>
            </form>
            <form action={updatePatientGoalStatusAction}>
              <input type="hidden" name="patient_id" value={patientId} />
              <input type="hidden" name="goal_id" value={goal.id} />
              <input type="hidden" name="status" value="inactive" />
              <button
                type="submit"
                className="text-[10px] px-2 py-0.5 rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                title="Retire this goal without marking achieved"
              >
                Retire
              </button>
            </form>
          </div>
        </div>
      ) : null}

      {!compact && goal.latest_observed_at ? (
        <div className="mt-1 text-[10px] text-zinc-500">
          last value {formatRelative(goal.latest_observed_at)}
        </div>
      ) : null}

      {compact && goal.status !== "active" ? (
        <form
          action={updatePatientGoalStatusAction}
          className="mt-1 inline"
        >
          <input type="hidden" name="patient_id" value={patientId} />
          <input type="hidden" name="goal_id" value={goal.id} />
          <input type="hidden" name="status" value="active" />
          <button
            type="submit"
            className="text-[10px] text-blue-600 dark:text-blue-400 hover:underline"
          >
            reactivate
          </button>
        </form>
      ) : null}
    </li>
  );
}
