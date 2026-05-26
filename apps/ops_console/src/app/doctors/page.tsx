import { orchestrator, type Doctor } from "@/lib/backend";

import {
  addDoctorAction,
  disconnectDoctorAction,
  setDoctorOnCallAction,
} from "./_actions";
import { AvailabilityChecker } from "./_components/AvailabilityChecker";

export const dynamic = "force-dynamic";

const STATUS_BADGE: Record<Doctor["oauth_status"], string> = {
  disconnected: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  connected: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  expired: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  revoked: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
};

function ResultBanner({
  status,
  detail,
}: {
  status?: string;
  detail?: string;
}) {
  if (!status) return null;
  const ok = status === "ok";
  return (
    <div
      className={
        "mb-4 rounded-lg border p-3 text-sm " +
        (ok
          ? "border-emerald-300 bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:border-emerald-800 dark:text-emerald-200"
          : "border-red-300 bg-red-50 text-red-800 dark:bg-red-950/40 dark:border-red-800 dark:text-red-200")
      }
    >
      {ok ? (
        <>OAuth completed successfully{detail ? ` (${detail})` : ""}.</>
      ) : (
        <>OAuth failed: <code className="font-mono text-xs">{detail ?? "unknown"}</code></>
      )}
    </div>
  );
}

export default async function DoctorsPage({
  searchParams,
}: {
  searchParams: Promise<{ oauth?: string; detail?: string }>;
}) {
  const params = await searchParams;
  // Snapshot the wall clock once at render entry. Server components only
  // render once per request, so a single reference time-anchors every
  // "synced N min ago" badge below. Calling Date.now() inside JSX trips
  // the React Compiler's impure-call rule (and would be a real source of
  // re-render drift in a client component); hoisting it to a top-level
  // const is the correct shape, but the lint rule can't tell server-only
  // contexts apart from client ones so we suppress it for this single line.
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();
  let doctors: Doctor[] = [];
  let error: string | null = null;
  try {
    doctors = await orchestrator.listDoctors();
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  return (
    <div className="space-y-8">
      <ResultBanner status={params.oauth} detail={params.detail} />

      <div>
        <h1 className="text-xl font-semibold">Doctors</h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Per-doctor Google Calendar OAuth. Connecting a calendar lets the
          booking agent read availability and create events on the doctor&apos;s
          primary calendar.
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t reach the orchestrator: <code className="font-mono text-xs">{error}</code>
        </div>
      ) : null}

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Add doctor
        </h2>
        <form
          action={addDoctorAction}
          className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 items-end"
        >
          <label className="flex flex-col gap-1 text-xs">
            Name
            <input
              name="name"
              required
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Email (Google account)
            <input
              name="email"
              type="email"
              required
              className="px-3 py-2 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            Phone (optional)
            <input
              name="phone"
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
          <button
            type="submit"
            className="px-4 py-2 rounded bg-zinc-900 text-white hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900"
          >
            Add
          </button>
        </form>
      </section>

      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Roster
        </h2>
        {doctors.length === 0 ? (
          <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
            No doctors yet. Add one above.
          </div>
        ) : (
          <div className="mt-3 space-y-4">
            {doctors.map((doc) => (
              <div
                key={doc.id}
                className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4"
              >
                <div className="flex items-start justify-between gap-4 flex-wrap">
                  <div>
                    <div className="font-medium">{doc.name}</div>
                    <div className="text-xs text-zinc-500">
                      {doc.email} · {doc.timezone} · calendar:{" "}
                      <span className="font-mono">{doc.calendar_id}</span>
                      {/* Inbound-sync indicator. Tells ops the
                          calendar_sync_sweep is actually firing
                          for this doctor. Null = never synced
                          (just connected, sweep hasn't run yet);
                          stale = sweep has been broken for this
                          doctor for a while. */}
                      {doc.oauth_status === "connected" ? (
                        <>
                          {" · "}
                          {doc.gcal_last_synced_at ? (
                            <span
                              className={
                                "text-[10px] " +
                                (nowMs -
                                  new Date(
                                    doc.gcal_last_synced_at,
                                  ).getTime() >
                                30 * 60 * 1000
                                  ? "text-amber-600 dark:text-amber-400"
                                  : "text-zinc-500")
                              }
                              title={`last calendar sync: ${doc.gcal_last_synced_at}`}
                            >
                              synced{" "}
                              {(() => {
                                const diff = Math.round(
                                  (nowMs -
                                    new Date(
                                      doc.gcal_last_synced_at,
                                    ).getTime()) /
                                    60000,
                                );
                                if (diff < 1) return "just now";
                                if (diff < 60) return `${diff}m ago`;
                                if (diff < 1440)
                                  return `${Math.round(diff / 60)}h ago`;
                                return `${Math.round(diff / 1440)}d ago`;
                              })()}
                            </span>
                          ) : (
                            <span className="text-[10px] text-zinc-400">
                              never synced
                            </span>
                          )}
                        </>
                      ) : null}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <a
                      href={`/doctors/${doc.id}/digest`}
                      className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                    >
                      Daily digest →
                    </a>
                    <span
                      className={
                        "px-2 py-0.5 rounded text-xs font-medium " +
                        STATUS_BADGE[doc.oauth_status]
                      }
                    >
                      {doc.oauth_status}
                    </span>
                    {/* On-call toggle. Doctors flagged on-call
                        appear in the critical-alert paging
                        fallback. We badge "no phone" when the
                        flag is set on a doctor without a
                        phone number — it's still allowed
                        (future rota features may use it) but
                        the pager can't reach them. */}
                    <form action={setDoctorOnCallAction} className="inline">
                      <input type="hidden" name="doctor_id" value={doc.id} />
                      <input
                        type="hidden"
                        name="on_call"
                        value={(!doc.is_on_call).toString()}
                      />
                      <button
                        type="submit"
                        className={
                          "px-2 py-0.5 rounded text-xs font-medium " +
                          (doc.is_on_call
                            ? "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-200"
                            : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300")
                        }
                        title={
                          doc.is_on_call
                            ? "Click to take this doctor off-call"
                            : "Click to mark on-call (paging fallback)"
                        }
                      >
                        {doc.is_on_call ? "🚨 on-call" : "off-call"}
                      </button>
                    </form>
                    {doc.is_on_call && !doc.phone ? (
                      <span
                        className="text-[10px] text-amber-700 dark:text-amber-300"
                        title="Doctor is flagged on-call but has no phone — pages will skip them"
                      >
                        ⚠ no phone
                      </span>
                    ) : null}
                    {doc.oauth_status === "connected" ? (
                      <form action={disconnectDoctorAction} className="inline">
                        <input type="hidden" name="doctor_id" value={doc.id} />
                        <button
                          type="submit"
                          className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                        >
                          Disconnect
                        </button>
                      </form>
                    ) : (
                      <a
                        href={`/api/google/oauth/start?doctor_id=${doc.id}`}
                        className="px-3 py-1 text-xs rounded bg-blue-600 text-white hover:bg-blue-500"
                      >
                        Connect Google Calendar
                      </a>
                    )}
                  </div>
                </div>
                {doc.oauth_status === "connected" ? (
                  <AvailabilityChecker doctorId={doc.id} timezone={doc.timezone} />
                ) : (
                  <div className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
                    Connect Google Calendar to query availability and book slots.
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
