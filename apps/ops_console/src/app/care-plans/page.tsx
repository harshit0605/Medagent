import {
  orchestrator,
  type CarePlan,
  type CarePlanCohortOption,
} from "@/lib/backend";

import {
  createCarePlanAction,
  deactivateCarePlanAction,
  reactivateCarePlanAction,
  updateCarePlanAction,
} from "./_actions";

export const dynamic = "force-dynamic";

const COHORT_LABEL: Record<string, string> = {
  cohort_diabetes: "Diabetes",
  cohort_cardiac: "Cardiac",
  cohort_fall_risk: "Fall risk",
};

function formatCohort(attr: string): string {
  return COHORT_LABEL[attr] ?? attr;
}

function planCohortLabel(plan: CarePlan): string {
  if (plan.cohort_tag_label) return plan.cohort_tag_label;
  if (plan.cohort_attr) return formatCohort(plan.cohort_attr);
  return "(no cohort)";
}

/** Encode the picker selection in one form-friendly string and decode
 * it server-side. "attr:cohort_diabetes" → boolean cohort,
 * "tag:42" → tag-based cohort. Done this way so a single <select> can
 * cover both kinds and the server action can route correctly. */
function cohortOptionValue(option: CarePlanCohortOption): string {
  if (option.kind === "boolean") return `attr:${option.cohort_attr}`;
  return `tag:${option.cohort_tag_id}`;
}

function CarePlanRow({ plan }: { plan: CarePlan }) {
  return (
    <tr
      className={
        "border-t border-zinc-200 dark:border-zinc-800 " +
        (plan.active ? "" : "opacity-60")
      }
    >
      <td className="px-4 py-3">
        <span
          className={
            "inline-block px-2 py-0.5 rounded text-xs font-medium " +
            (plan.cohort_tag_id !== null
              ? "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-200"
              : "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200")
          }
          title={
            plan.cohort_tag_id !== null
              ? `tag · ${plan.cohort_tag_slug}`
              : "legacy boolean cohort"
          }
        >
          {planCohortLabel(plan)}
        </span>
      </td>
      <td className="px-4 py-3 font-medium">{plan.test_name}</td>
      <td className="px-4 py-3 text-sm text-zinc-700 dark:text-zinc-300">
        every {plan.cadence_days}d
      </td>
      <td className="px-4 py-3 text-sm text-zinc-700 dark:text-zinc-300">
        +{plan.due_in_days}d
      </td>
      <td className="px-4 py-3 text-xs text-zinc-500 max-w-md">
        {plan.notes ?? <span className="text-zinc-400">—</span>}
      </td>
      <td className="px-4 py-3">
        {plan.active ? (
          <span className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
            active
          </span>
        ) : (
          <span className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-300">
            inactive
          </span>
        )}
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap items-end gap-2">
          <form className="flex items-end gap-2">
            <input type="hidden" name="plan_id" value={plan.id} />
            <label className="text-[11px] text-zinc-500 flex flex-col gap-0.5">
              cadence
              <input
                type="number"
                name="cadence_days"
                min={1}
                max={3650}
                defaultValue={plan.cadence_days}
                className="w-20 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
              />
            </label>
            <label className="text-[11px] text-zinc-500 flex flex-col gap-0.5">
              due in
              <input
                type="number"
                name="due_in_days"
                min={0}
                max={365}
                defaultValue={plan.due_in_days}
                className="w-16 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
              />
            </label>
            <label className="text-[11px] text-zinc-500 flex flex-col gap-0.5 flex-1 min-w-[180px]">
              notes
              <input
                type="text"
                name="notes"
                defaultValue={plan.notes ?? ""}
                placeholder="Clinical rationale (optional)"
                className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
              />
            </label>
            <button
              type="submit"
              formAction={updateCarePlanAction}
              className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 text-xs hover:bg-zinc-50 dark:hover:bg-zinc-800"
            >
              Save
            </button>
            {plan.active ? (
              <button
                type="submit"
                formAction={deactivateCarePlanAction}
                className="px-2 py-1 rounded-md border border-red-300 dark:border-red-800 text-xs text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40"
              >
                Deactivate
              </button>
            ) : (
              <button
                type="submit"
                formAction={reactivateCarePlanAction}
                className="px-2 py-1 rounded-md border border-emerald-300 dark:border-emerald-800 text-xs text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-950/40"
              >
                Reactivate
              </button>
            )}
          </form>
        </div>
      </td>
    </tr>
  );
}

export default async function CarePlansPage() {
  let plans: CarePlan[] = [];
  let cohorts: CarePlanCohortOption[] = [];
  let error: string | null = null;
  try {
    [plans, cohorts] = await Promise.all([
      orchestrator.listCarePlans({ include_inactive: true }),
      orchestrator.listCarePlanCohorts(),
    ]);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Care plans</h1>
        <p className="mt-1 text-sm text-zinc-500 max-w-2xl">
          Standing-orders for cohort patients. The scheduler runs every
          6h and materializes a <code>lab_followup</code> for any
          patient in the cohort whose last matching test is older than
          the cadence — then the existing reminder + ticket flow takes
          over.
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load care plans.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      ) : null}

      {/* Existing plans */}
      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden bg-white dark:bg-zinc-900">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
            <tr>
              <th className="text-left px-4 py-2">Cohort</th>
              <th className="text-left px-4 py-2">Test</th>
              <th className="text-left px-4 py-2">Cadence</th>
              <th className="text-left px-4 py-2">Due in</th>
              <th className="text-left px-4 py-2">Notes</th>
              <th className="text-left px-4 py-2">Status</th>
              <th className="text-left px-4 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {plans.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-6 text-center text-sm text-zinc-500"
                >
                  No care plans yet. Add one below to start sweeping
                  cohorts for that test.
                </td>
              </tr>
            ) : (
              plans.map((plan) => <CarePlanRow key={plan.id} plan={plan} />)
            )}
          </tbody>
        </table>
      </section>

      {/* Add new plan */}
      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Add a new plan
        </h2>
        <form className="mt-3 grid grid-cols-1 md:grid-cols-6 gap-3 items-end">
          <label className="text-xs text-zinc-500 flex flex-col gap-1">
            Cohort
            <select
              name="cohort_choice"
              required
              defaultValue=""
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            >
              <option value="" disabled>
                Select cohort…
              </option>
              {cohorts.filter((c) => c.kind === "boolean").length > 0 ? (
                <optgroup label="Built-in cohorts">
                  {cohorts
                    .filter((c) => c.kind === "boolean")
                    .map((c) => (
                      <option
                        key={`attr-${c.cohort_attr}`}
                        value={cohortOptionValue(c)}
                      >
                        {c.label}
                      </option>
                    ))}
                </optgroup>
              ) : null}
              {cohorts.filter((c) => c.kind === "tag").length > 0 ? (
                <optgroup label="Cohort tags">
                  {cohorts
                    .filter((c) => c.kind === "tag")
                    .map((c) => (
                      <option
                        key={`tag-${c.cohort_tag_id}`}
                        value={cohortOptionValue(c)}
                      >
                        {c.label}
                      </option>
                    ))}
                </optgroup>
              ) : null}
            </select>
          </label>
          <label className="text-xs text-zinc-500 flex flex-col gap-1 md:col-span-2">
            Test name
            <input
              type="text"
              name="test_name"
              required
              placeholder="e.g. HbA1c, Lipid panel"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
          <label className="text-xs text-zinc-500 flex flex-col gap-1">
            Cadence (days)
            <input
              type="number"
              name="cadence_days"
              required
              min={1}
              max={3650}
              defaultValue={180}
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
          <label className="text-xs text-zinc-500 flex flex-col gap-1">
            Due in (days)
            <input
              type="number"
              name="due_in_days"
              min={0}
              max={365}
              defaultValue={14}
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
          <button
            type="submit"
            formAction={createCarePlanAction}
            className="px-3 py-2 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white text-sm"
          >
            Add plan
          </button>
          <label className="text-xs text-zinc-500 flex flex-col gap-1 md:col-span-5">
            Notes (optional)
            <input
              type="text"
              name="notes"
              placeholder="Clinical rationale, reference guideline, etc."
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
          <label className="text-xs text-zinc-500 flex flex-col gap-1">
            Created by
            <input
              type="text"
              name="created_by"
              placeholder="Dr handle / role"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
        </form>
      </section>
    </div>
  );
}
