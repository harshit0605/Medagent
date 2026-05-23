import { orchestrator, type CohortTag } from "@/lib/backend";

import {
  createCohortTagAction,
  deactivateCohortTagAction,
  reactivateCohortTagAction,
  updateCohortTagAction,
} from "./_actions";

export const dynamic = "force-dynamic";

export default async function CohortTagsPage() {
  let tags: CohortTag[] = [];
  let error: string | null = null;
  try {
    tags = await orchestrator.listCohortTags({ include_inactive: true });
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Cohort tags</h1>
        <p className="mt-1 text-sm text-zinc-500 max-w-2xl">
          Clinician-authored cohort labels (e.g. &quot;Post-MI&quot;,
          &quot;Pregnancy 3T&quot;). Assign tags on a patient&apos;s detail
          page; reference them from a care plan to materialise standing
          orders against the cohort.
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load cohort tags.
          <div className="mt-1 font-mono text-xs">{error}</div>
        </div>
      ) : null}

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden bg-white dark:bg-zinc-900">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 dark:bg-zinc-950 text-xs uppercase text-zinc-500">
            <tr>
              <th className="text-left px-4 py-2">Label</th>
              <th className="text-left px-4 py-2">Slug</th>
              <th className="text-left px-4 py-2">Description</th>
              <th className="text-left px-4 py-2">Patients</th>
              <th className="text-left px-4 py-2">Status</th>
              <th className="text-left px-4 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {tags.length === 0 ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-6 text-center text-sm text-zinc-500"
                >
                  No cohort tags yet. Add one below to start grouping
                  patients beyond the legacy diabetes / cardiac / fall
                  risk flags.
                </td>
              </tr>
            ) : (
              tags.map((tag) => (
                <tr
                  key={tag.id}
                  className={
                    "border-t border-zinc-200 dark:border-zinc-800 " +
                    (tag.active ? "" : "opacity-60")
                  }
                >
                  <td className="px-4 py-3 font-medium">{tag.label}</td>
                  <td className="px-4 py-3 text-xs font-mono text-zinc-500">
                    {tag.slug}
                  </td>
                  <td className="px-4 py-3 text-xs text-zinc-500 max-w-md">
                    {tag.description ?? <span className="text-zinc-400">—</span>}
                  </td>
                  <td className="px-4 py-3 text-sm tabular-nums">
                    {tag.patient_count}
                  </td>
                  <td className="px-4 py-3">
                    {tag.active ? (
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
                    <form className="flex flex-wrap items-end gap-2">
                      <input type="hidden" name="tag_id" value={tag.id} />
                      <label className="text-[11px] text-zinc-500 flex flex-col gap-0.5">
                        Label
                        <input
                          type="text"
                          name="label"
                          defaultValue={tag.label}
                          className="w-40 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
                        />
                      </label>
                      <label className="text-[11px] text-zinc-500 flex flex-col gap-0.5 flex-1 min-w-[180px]">
                        Description
                        <input
                          type="text"
                          name="description"
                          defaultValue={tag.description ?? ""}
                          placeholder="Clinical definition (optional)"
                          className="w-full rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-1 text-xs"
                        />
                      </label>
                      <button
                        type="submit"
                        formAction={updateCohortTagAction}
                        className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 text-xs hover:bg-zinc-50 dark:hover:bg-zinc-800"
                      >
                        Save
                      </button>
                      {tag.active ? (
                        <button
                          type="submit"
                          formAction={deactivateCohortTagAction}
                          className="px-2 py-1 rounded-md border border-red-300 dark:border-red-800 text-xs text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40"
                        >
                          Deactivate
                        </button>
                      ) : (
                        <button
                          type="submit"
                          formAction={reactivateCohortTagAction}
                          className="px-2 py-1 rounded-md border border-emerald-300 dark:border-emerald-800 text-xs text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-950/40"
                        >
                          Reactivate
                        </button>
                      )}
                    </form>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Add a new tag
        </h2>
        <form className="mt-3 grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
          <label className="text-xs text-zinc-500 flex flex-col gap-1">
            Label
            <input
              type="text"
              name="label"
              required
              placeholder="e.g. Post-MI"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
          <label className="text-xs text-zinc-500 flex flex-col gap-1">
            Slug (optional)
            <input
              type="text"
              name="slug"
              placeholder="auto-generated from label"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm font-mono"
            />
          </label>
          <label className="text-xs text-zinc-500 flex flex-col gap-1 md:col-span-2">
            Description
            <input
              type="text"
              name="description"
              placeholder="Clinical definition / inclusion criteria"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 p-2 text-sm"
            />
          </label>
          <input type="hidden" name="created_by" defaultValue="ops" />
          <button
            type="submit"
            formAction={createCohortTagAction}
            className="md:col-span-1 justify-self-start px-3 py-2 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white text-sm"
          >
            Add tag
          </button>
        </form>
      </section>
    </div>
  );
}
