import Link from "next/link";

import {
  orchestrator,
  type PatientTimeline as PatientTimelineData,
  type TimelineEvent,
  type TimelineEventKind,
} from "@/lib/backend";

// Visual config per kind. Centralised here so the renderer
// stays dumb — adding a new kind is one row in this map plus
// the backend gather. The icon column is intentionally string
// emoji, not an icon font: ops console is intentionally
// dependency-light.
const KIND_STYLE: Record<
  TimelineEventKind,
  { icon: string; label: string; tone: string }
> = {
  message_inbound: {
    icon: "💬",
    label: "Patient",
    tone: "border-blue-200 dark:border-blue-900/40 bg-blue-50/40 dark:bg-blue-950/20",
  },
  message_outbound: {
    icon: "🤖",
    label: "Bot",
    tone: "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900",
  },
  dose_taken: {
    icon: "✅",
    label: "Dose taken",
    tone: "border-emerald-200 dark:border-emerald-900/40 bg-emerald-50/40 dark:bg-emerald-950/20",
  },
  dose_missed: {
    icon: "❌",
    label: "Dose missed",
    tone: "border-red-200 dark:border-red-900/40 bg-red-50/40 dark:bg-red-950/20",
  },
  dose_skipped: {
    icon: "⏭️",
    label: "Dose skipped",
    tone: "border-amber-200 dark:border-amber-900/40 bg-amber-50/40 dark:bg-amber-950/20",
  },
  dose_delayed: {
    icon: "⏰",
    label: "Dose delayed",
    tone: "border-blue-200 dark:border-blue-900/40 bg-blue-50/40 dark:bg-blue-950/20",
  },
  side_effect: {
    icon: "⚠️",
    label: "Side effect",
    tone: "border-red-200 dark:border-red-900/40 bg-red-50/40 dark:bg-red-950/20",
  },
  appointment_booked: {
    icon: "📅",
    label: "Appt booked",
    tone: "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900",
  },
  appointment_completed: {
    icon: "📅",
    label: "Appt completed",
    tone: "border-emerald-200 dark:border-emerald-900/40 bg-emerald-50/40 dark:bg-emerald-950/20",
  },
  appointment_cancelled: {
    icon: "📅",
    label: "Appt cancelled",
    tone: "border-zinc-200 dark:border-zinc-800 bg-zinc-50/40 dark:bg-zinc-950/20",
  },
  appointment_no_show: {
    icon: "📅",
    label: "Appt no-show",
    tone: "border-amber-200 dark:border-amber-900/40 bg-amber-50/40 dark:bg-amber-950/20",
  },
  lab_booked: {
    icon: "🧪",
    label: "Lab booked",
    tone: "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900",
  },
  lab_completed: {
    icon: "🧪",
    label: "Lab completed",
    tone: "border-emerald-200 dark:border-emerald-900/40 bg-emerald-50/40 dark:bg-emerald-950/20",
  },
  lab_reviewed: {
    icon: "🧪",
    label: "Lab reviewed",
    tone: "border-emerald-200 dark:border-emerald-900/40 bg-emerald-50/40 dark:bg-emerald-950/20",
  },
  clinical_alert_high: {
    icon: "⚠️",
    label: "Clinical alert (high)",
    tone: "border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30",
  },
  clinical_alert_critical: {
    icon: "🚨",
    label: "Clinical alert (critical)",
    tone: "border-red-400 dark:border-red-700 bg-red-50 dark:bg-red-950/40",
  },
};

function formatRelative(iso: string): string {
  // Relative time gives the doctor a quick "how recent?" read
  // without forcing them to compute "is 2026-05-01 last week
  // or last month?". Falls back to absolute for dates older
  // than a fortnight where relative loses precision.
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diff = now - then;
  if (diff < 0) {
    // Future event (e.g. an upcoming appointment within the
    // window). Show absolute.
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 14) return `${days}d ago`;
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function detailLink(event: TimelineEvent): string | null {
  // Click-through targets — when the timeline event
  // corresponds to a row that has its own ops surface, expose
  // a deep link. Other kinds are leaf (e.g. dose events) and
  // don't navigate.
  if (event.kind === "side_effect") {
    const ticketId = event.detail.ticket_id;
    if (typeof ticketId === "number" || typeof ticketId === "string")
      return `/tickets/${ticketId}`;
  }
  if (
    event.kind === "clinical_alert_high" ||
    event.kind === "clinical_alert_critical"
  ) {
    // Alerts don't have a per-id detail page yet; the queue
    // page is the only surface, and the alert id is shown
    // there. Future "/clinical-alerts/{id}" route would
    // replace this.
    return "/clinical-alerts";
  }
  if (
    event.kind === "lab_booked" ||
    event.kind === "lab_completed" ||
    event.kind === "lab_reviewed"
  ) {
    // Labs don't have a dedicated detail page yet — keep this
    // wired for when one lands.
    return null;
  }
  if (
    event.kind === "appointment_booked" ||
    event.kind === "appointment_completed" ||
    event.kind === "appointment_cancelled" ||
    event.kind === "appointment_no_show"
  ) {
    const link = event.detail.calendar_html_link;
    if (typeof link === "string" && link.length > 0) return link;
  }
  return null;
}

function isExternalLink(href: string): boolean {
  return /^https?:\/\//i.test(href);
}

export async function PatientTimeline({
  patientId,
}: {
  patientId: number;
}) {
  let data: PatientTimelineData | null = null;
  let error: string | null = null;
  try {
    data = await orchestrator.getPatientTimeline(patientId, {
      limit: 60,
    });
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (error) {
    return (
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Timeline (last 30d)
        </h2>
        <div className="mt-3 rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 p-3 text-sm text-red-800 dark:text-red-200">
          Couldn&apos;t load timeline.{" "}
          <code className="font-mono text-xs">{error}</code>
        </div>
      </section>
    );
  }

  if (!data || data.events.length === 0) {
    return (
      <section>
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Timeline (last 30d)
        </h2>
        <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800 p-6 text-center text-sm text-zinc-500">
          No activity in the last 30 days.
        </div>
      </section>
    );
  }

  return (
    <section>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-sm font-medium text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">
          Timeline (last 30d)
        </h2>
        <span className="text-xs text-zinc-500">
          {data.events.length} events
        </span>
      </div>
      <ol className="mt-3 space-y-2">
        {data.events.map((event, idx) => {
          const style = KIND_STYLE[event.kind] ?? {
            icon: "•",
            label: event.kind,
            tone: "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900",
          };
          const link = detailLink(event);
          return (
            <li
              key={`${event.occurred_at}-${event.kind}-${idx}`}
              className={`rounded-lg border ${style.tone} p-3 text-sm`}
            >
              <div className="flex items-baseline justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 min-w-0">
                  <span aria-hidden className="text-base shrink-0">
                    {style.icon}
                  </span>
                  <span className="font-medium text-zinc-900 dark:text-zinc-100 truncate">
                    {event.title}
                  </span>
                  <span className="text-[10px] uppercase tracking-wide text-zinc-500 px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800">
                    {style.label}
                  </span>
                </div>
                <span
                  className="text-xs text-zinc-500 tabular-nums shrink-0"
                  title={new Date(event.occurred_at).toISOString()}
                >
                  {formatRelative(event.occurred_at)}
                </span>
              </div>
              {event.body ? (
                <p className="mt-1.5 ml-7 text-zinc-700 dark:text-zinc-300 whitespace-pre-line">
                  {event.body}
                </p>
              ) : null}
              {link ? (
                isExternalLink(link) ? (
                  <a
                    className="mt-1.5 ml-7 inline-block text-xs text-blue-600 hover:underline dark:text-blue-400"
                    href={link}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    open →
                  </a>
                ) : (
                  <Link
                    className="mt-1.5 ml-7 inline-block text-xs text-blue-600 hover:underline dark:text-blue-400"
                    href={link}
                  >
                    details →
                  </Link>
                )
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
