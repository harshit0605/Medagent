/**
 * Server-side HTTP client for the Medagent backends.
 *
 * All callers must run on the Next.js server (Server Components, Server
 * Actions, Route Handlers) — these helpers read env vars (incl. the
 * ORCHESTRATOR_API_KEY secret) and do not include any browser-side fallback.
 * fetch is `cache: "no-store"` so the dashboard always reflects live state.
 *
 * The `server-only` import is a build-time tripwire: if a Client Component
 * ever imports a runtime value from this module, the build fails. Client
 * components must go through a Server Action instead (see each route's
 * `_actions.ts`). Type-only imports (`import type { ... }`) are erased by the
 * compiler and remain safe.
 */

import "server-only";

import { actorHeaders } from "./operator-signature";

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? "http://localhost:8002";
const SCHEDULER_URL = process.env.SCHEDULER_URL ?? "http://localhost:8003";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8001";
// Shared secrets sent to each backend when it enforces auth (the matching
// *_API_KEY set on both sides). Omitted when unset. The gateway's /send + /logs
// are protected separately from the orchestrator.
const ORCHESTRATOR_API_KEY = process.env.ORCHESTRATOR_API_KEY;
const GATEWAY_API_KEY = process.env.GATEWAY_API_KEY;

type FetchOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | undefined>;
  // Per-call timeout. Defaults to 30s — generous enough for the slowest
  // endpoints (DSAR export, LLM draft) while still bounding a hung backend
  // so a request can't pin a server worker indefinitely.
  timeoutMs?: number;
  // Operator identity for privileged endpoints (DSAR export, erasure,
  // pause/unpause, ticket lifecycle, exemption grant/revoke). When set we
  // add X-Ops-Actor and, when OPS_ACTOR_SIGNING_KEY is configured, the
  // HMAC-SHA256 signature in X-Ops-Actor-Signature. The orchestrator
  // verifies the signature when OPS_ACTOR_SIGNATURE_REQUIRED=1.
  signedActor?: string;
};

const DEFAULT_TIMEOUT_MS = 30_000;

async function call<T>(base: string, path: string, opts: FetchOptions = {}): Promise<T> {
  const url = new URL(path, base);
  if (opts.query) {
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined && v !== null && v !== "") {
        url.searchParams.set(k, String(v));
      }
    }
  }
  const headers: Record<string, string> = {};
  if (opts.body) headers["content-type"] = "application/json";
  // Authenticate calls when the target backend's shared secret is configured.
  if (ORCHESTRATOR_API_KEY && base === ORCHESTRATOR_URL) {
    headers["x-api-key"] = ORCHESTRATOR_API_KEY;
  } else if (GATEWAY_API_KEY && base === GATEWAY_URL) {
    headers["x-api-key"] = GATEWAY_API_KEY;
  }
  // Attach (and possibly sign) the operator-actor headers for privileged
  // calls. No-op when signedActor is unset.
  Object.assign(headers, actorHeaders(opts.signedActor));
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const init: RequestInit = {
    method: opts.method ?? "GET",
    cache: "no-store",
    headers: Object.keys(headers).length > 0 ? headers : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  };
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    // AbortSignal.timeout aborts with a DOMException named "TimeoutError".
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error(
        `${init.method} ${url.pathname} timed out after ${timeoutMs}ms`,
      );
    }
    throw err;
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${init.method} ${url.pathname} ${res.status}: ${text || res.statusText}`);
  }
  // 204 No Content (e.g. DELETE) — nothing to parse.
  if (res.status === 204) return null as T;
  // Guard JSON parsing: a 200 with an empty or non-JSON body (a stray proxy
  // error page, an HTML 200) would otherwise throw an opaque SyntaxError.
  const raw = await res.text();
  if (!raw) return null as T;
  try {
    return JSON.parse(raw) as T;
  } catch {
    throw new Error(
      `${init.method} ${url.pathname}: expected JSON but got ` +
        `${res.headers.get("content-type") ?? "an unparseable body"}`,
    );
  }
}

// ---- Orchestrator ----------------------------------------------------------

export type OpsTicket = {
  ticket_id: string;
  patient_id: string;
  patient_db_id: number | null;
  patient_full_name: string | null;
  category: string;
  priority: string;
  sla_minutes: number;
  status: "open" | "acknowledged" | "resolved";
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  notes: string | null;
  assigned_to: string | null;
  snoozed_until: string | null;
  sla_due_at: string;
  is_overdue: boolean;
  is_snoozed: boolean;
  // First-cross SLA breach timestamp stamped by the breach sweep.
  // Persists past resolution so analytics can count breaches; UI uses
  // it to render a permanent "breached" indicator distinct from the
  // live `is_overdue` flag.
  sla_breached_at: string | null;
};

export type ProgramMetrics = {
  adherence_rate: number;
  refill_risk_rate: number;
  followup_closure_rate: number;
};

export type DashboardAlerts = {
  regimens_running_low: number;
  missed_dose_escalations_open: number;
  refill_help_open: number;
  labs_overdue: number;
  lab_help_open: number;
  prescriptions_pending: number;
  tickets_sla_overdue: number;
  care_gaps_open: number;
};

// Delivery state of outbound WhatsApp messages over the dashboard
// window (default 24h). Computed by the orchestrator joining
// message_log → whatsapp_message_statuses on wamid.
export type DeliverySummary = {
  since: string;
  total_outbound: number;
  by_status: {
    delivered: number;
    read: number;
    sent_only: number;
    failed: number;
    failed_pre_meta: number;
    no_status_yet: number;
  };
  delivery_rate: number;
  failure_rate: number;
  by_payload_kind: Record<
    string,
    { total: number; delivered: number; failed: number }
  >;
  top_failure_codes: Array<{
    code: number;
    title: string | null;
    count: number;
  }>;
};

// Per-template breakdown — one row per distinct template_name in the
// dashboard window. Surfaces single-template failures that the
// aggregate ``DeliverySummary`` would average away.
export type DeliveryByTemplateRow = {
  template_name: string;
  total: number;
  delivered: number;
  failed: number;
  failed_pre_meta: number;
  delivery_rate: number;
  failure_rate: number;
};

export type Dashboard = {
  program_metrics: ProgramMetrics;
  queue: { open: number; acknowledged: number; resolved: number; total: number };
  alerts: DashboardAlerts;
  delivery: DeliverySummary;
  delivery_by_template: DeliveryByTemplateRow[];
};

export type AdherenceSnapshot = {
  total: number;
  taken: number;
  missed: number;
  skipped: number;
  delayed: number;
  scheduled: number;
  rate: number;
};

export type RecapFunnel = {
  draft: number;
  sent: number;
  acknowledged: number;
  questioned: number;
  sent_total: number;
  ack_rate: number;
};

export type InboxComposition = {
  by_category: Record<string, number>;
  by_urgency: Record<string, number>;
  by_input_kind: Record<string, number>;
};

export type OpsQueueAnalytics = {
  open_total: number;
  by_priority: Record<string, number>;
  opened_in_window: number;
  resolved_in_window: number;
  median_resolve_minutes: number | null;
};

export type AdherenceBucket = {
  date: string;
  taken: number;
  missed: number;
  skipped: number;
  delayed: number;
  scheduled: number;
  rate: number;
};

export type InboxBucket = {
  date: string;
  total: number;
  critical: number;
  high: number;
  medium: number;
  low: number;
};

export type RecapBucket = {
  date: string;
  sent: number;
  acked: number;
};

export type TicketBucket = {
  date: string;
  opened: number;
  resolved: number;
};

export type AnalyticsTimeseries = {
  window_days: number;
  adherence: AdherenceBucket[];
  inbox: InboxBucket[];
  recap: RecapBucket[];
  tickets: TicketBucket[];
};

export type AnalyticsSnapshot = {
  window_days: number;
  since: string;
  adherence: AdherenceSnapshot;
  recap_funnel: RecapFunnel;
  inbox: InboxComposition;
  ops_queue: OpsQueueAnalytics;
  timeseries: AnalyticsTimeseries;
};

export type Heartbeat = {
  component: string;
  last_run_at: string;
  last_outcome: "ok" | "error" | "skipped";
  details: Record<string, unknown>;
  consecutive_errors: number;
  seconds_since_last_run: number;
  is_stale: boolean;
};

export type HealthSummary = {
  components: Heartbeat[];
  failed_events_24h: number;
  pending_overdue: number;
  stuck_components: number;
  error_components: number;
};

export type RouteResponse = {
  intent: string;
  flow_action: string;
  risk_level: string;
  escalation_required: boolean;
  audit_reasons: string[];
  message_out: {
    patient_id: string | null;
    body: string | null;
    use_template: boolean;
    template_name: string | null;
    quick_replies: { id: string; title: string }[];
    buttons: { id: string; label: string; action: string }[];
  };
  // The fields below are present on the normal routing path but OMITTED by the
  // short-circuit branches: the rate-limit gate and the inbound-replay dedupe
  // both return a reduced payload (empty-body message_out + a marker). Optional
  // so consumers must handle their absence rather than crash on undefined.
  policy?: { in_customer_service_window: boolean; use_template: boolean; reason: string };
  policy_reason_codes?: string[];
  ticket_id?: string | null;
  runner?: "langgraph" | "sync_fallback";
  rate_limited?: boolean;
  rate_limit_count?: number;
  rate_limit_window_minutes?: number;
  deduped?: boolean;
};

export type Doctor = {
  id: number;
  name: string;
  email: string;
  phone: string | null;
  timezone: string;
  calendar_id: string;
  oauth_status: "disconnected" | "connected" | "expired" | "revoked";
  google_user_id: string | null;
  // Wall-clock of the last successful inbound calendar sync —
  // null means we've never synced, or the doctor was just
  // reconnected. UI renders "synced N min ago" off this.
  gcal_last_synced_at: string | null;
  // Critical-alert paging fanout fallback. Doctors with this
  // flag are paged when a patient has no primary-doctor
  // history. Doctors without ``phone`` can be flagged but
  // won't actually receive pages.
  is_on_call: boolean;
  created_at: string;
  updated_at: string;
};

export type AvailabilitySlot = { start: string; end: string };

export type DoctorAvailability = {
  doctor_id: number;
  timezone: string;
  requested_window: AvailabilitySlot;
  duration_minutes: number;
  busy: AvailabilitySlot[];
  free: AvailabilitySlot[];
};

export type CalendarEvent = {
  event_id: string;
  calendar_id: string;
  summary: string | null;
  description: string | null;
  start: string;
  end: string;
  html_link: string | null;
  attendees: string[];
  status: string | null;
};

export type PatientSummary = {
  id: number;
  full_name: string;
  phone: string;
  cohort_diabetes: boolean;
  cohort_cardiac: boolean;
  cohort_fall_risk: boolean;
  active_regimen_count: number;
  upcoming_appointment_count: number;
  open_ticket_count: number;
  created_at: string;
};

export type Regimen = {
  id: number;
  patient_id: number;
  medication_name: string;
  dose: string;
  schedule: {
    type?: string;
    times?: string[];
    timezone?: string;
    frequency?: string;
  } & Record<string, unknown>;
  starts_on: string | null;
  ends_on: string | null;
  strict_timing: boolean;
  supply_days_initial: number | null;
  supply_started_on: string | null;
  days_of_supply_remaining: number | null;
  created_at: string;
  updated_at: string;
};

export type AdherenceEventEntry = {
  id: number;
  regimen_id: number | null;
  medication_name: string | null;
  scheduled_at: string;
  status: "scheduled" | "taken" | "missed" | "skipped" | "delayed";
  confirmed_at: string | null;
};

export type RefillEventEntry = {
  id: number;
  regimen_id: number | null;
  medication_name: string | null;
  scheduled_for: string;
  dispatched_at: string | null;
  stage: string | null;
  status: "pending" | "dispatched" | "skipped" | "failed";
  label: string;
};

export type LabFollowup = {
  id: number;
  patient_id: number;
  test_name: string;
  status: "due" | "booked" | "completed" | "reviewed";
  due_by: string | null;
  notes: string | null;
  booked_at: string | null;
  completed_at: string | null;
  reviewed_at: string | null;
  days_until_due: number | null;
  is_overdue: boolean;
  created_at: string;
  updated_at: string;
};

export type ParsedRegimen = {
  medication_name: string;
  dose: string;
  times_of_day: string[];
  frequency_text?: string | null;
  duration_days?: number | null;
  notes?: string | null;
};

export type Prescription = {
  id: number;
  patient_id: number;
  patient_full_name: string | null;
  source_upload_url: string;
  public_path: string | null;
  parsed_payload: Record<string, unknown>;
  parsed_regimens: ParsedRegimen[];
  vision_parse_failed: boolean;
  confidence: "high" | "medium" | "low" | null;
  illegible: boolean;
  summary: string | null;
  status: "pending" | "verified" | "rejected";
  verified_at: string | null;
  verified_by: string | null;
  created_at: string;
  updated_at: string;
};

export type AppointmentSummary = {
  id: number;
  doctor_id: number;
  doctor_name: string | null;
  scheduled_for: string;
  end_at: string;
  status: string;
  calendar_html_link: string | null;
};

export type AppointmentDetail = {
  id: number;
  patient_id: number;
  doctor_id: number;
  doctor_name: string | null;
  doctor_timezone: string | null;
  patient_full_name: string | null;
  scheduled_for: string;
  end_at: string;
  status: string;
  summary: string | null;
  notes: string | null;
  calendar_html_link: string | null;
};

export type RecapMedItem = {
  regimen_id?: number | null;
  name: string;
  instructions?: string | null;
  change?: string | null;
};

export type RecapLabItem = {
  lab_followup_id?: number | null;
  test_name: string;
};

export type RecapStructuredPayload = {
  meds_added: RecapMedItem[];
  meds_changed: RecapMedItem[];
  meds_stopped: RecapMedItem[];
  labs_ordered: RecapLabItem[];
  next_followup_in_days: number | null;
  red_flags: string[];
};

export type InboundClassification = {
  id: number;
  message_id: string | null;
  patient_phone: string;
  patient_db_id: number | null;
  patient_full_name: string | null;
  inbound_text: string | null;
  input_kind: "text" | "voice" | "image" | "button";
  category:
    | "clinical_question"
    | "administrative"
    | "billing"
    | "scheduling"
    | "faq"
    | "social"
    | "unsafe"
    | "action_tap"
    | "unknown";
  summary: string | null;
  urgency: "critical" | "high" | "medium" | "low";
  handler_used: string | null;
  response_text: string | null;
  escalated: boolean;
  ticket_id: number | null;
  created_at: string;
  // Bot-reply quality feedback. +1 = thumbs-up, -1 = thumbs-down,
  // null = no rating. The note + by + at fields carry the ops
  // context for thumbs-down reviews ("the bot misclassified this
  // as scheduling, should have been clinical_question").
  feedback_rating: number | null;
  feedback_note: string | null;
  feedback_by: string | null;
  feedback_at: string | null;
  // Triage classifier verdict — non-null when this message
  // tripped a clinical_alerts row. UI renders a severity
  // badge inline so doctors scanning inbox spot alert-tied
  // messages without bouncing to /clinical-alerts.
  clinical_severity: "high" | "critical" | null;
};

export type CarePlan = {
  id: number;
  cohort_attr: string | null;
  cohort_tag_id: number | null;
  cohort_tag_label: string | null;
  cohort_tag_slug: string | null;
  test_name: string;
  cadence_days: number;
  due_in_days: number;
  active: boolean;
  notes: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

export type CarePlanCohortOption = {
  kind: "boolean" | "tag";
  cohort_attr: string | null;
  cohort_tag_id: number | null;
  label: string;
  description: string | null;
};

export type CohortTag = {
  id: number;
  slug: string;
  label: string;
  description: string | null;
  active: boolean;
  created_by: string | null;
  patient_count: number;
  created_at: string;
  updated_at: string;
};

export type PatientCohortTagAssignment = {
  id: number;
  patient_id: number;
  cohort_tag_id: number;
  cohort_tag_slug: string;
  cohort_tag_label: string;
  assigned_by: string | null;
  assigned_at: string;
};

export type Caregiver = {
  id: number;
  patient_id: number;
  full_name: string;
  phone: string;
  relationship_to_patient: string | null;
  consent_status: "pending" | "confirmed" | "declined" | "revoked";
  consent_confirmed_at: string | null;
  consent_confirmed_by: string | null;
  notify_on_recap: boolean;
  active: boolean;
  created_at: string;
  updated_at: string;
};

export type CarePlanExemption = {
  id: number;
  patient_id: number;
  care_plan_id: number;
  care_plan_cohort: string | null;
  care_plan_test_name: string | null;
  reason: string;
  expires_at: string | null;
  revoked_at: string | null;
  created_by: string | null;
  revoked_by: string | null;
  created_at: string;
  updated_at: string;
  is_active: boolean;
};

export type PreVisitInboxItem = {
  id: number;
  created_at: string;
  category: string;
  urgency: "critical" | "high" | "medium" | "low";
  summary: string | null;
  inbound_text: string | null;
  handler_used: string | null;
  escalated: boolean;
};

export type PreVisitTicket = {
  ticket_id: string;
  category: string;
  priority: string;
  status: string;
  is_overdue: boolean;
  is_snoozed: boolean;
  created_at: string;
  sla_breached_at: string | null;
};

export type PreVisitCohortTag = {
  cohort_tag_id: number;
  label: string;
  slug: string;
};

export type PreVisitExemption = {
  id: number;
  care_plan_id: number;
  care_plan_test_name: string | null;
  reason: string;
  expires_at: string | null;
};

export type PreVisitRecapExcerpt = {
  appointment_id: number;
  appointment_date: string;
  summary: string;
  status: "sent" | "acknowledged" | "questioned" | "draft";
};

export type PreVisitSummary = {
  appointment: AppointmentDetail;
  patient: PatientSummary;
  cohort_flags: string[];
  cohort_tags: PreVisitCohortTag[];
  regimens: Regimen[];
  adherence: AdherenceSummary;
  open_lab_followups: LabFollowup[];
  open_tickets: PreVisitTicket[];
  active_exemptions: PreVisitExemption[];
  recent_inbox: PreVisitInboxItem[];
  last_recap: PreVisitRecapExcerpt | null;
  has_caregiver_cc: boolean;
};

export type AppointmentRecap = {
  id: number;
  appointment_id: number;
  patient_id: number;
  doctor_id: number;
  doctor_notes: string | null;
  structured_payload: RecapStructuredPayload | Record<string, unknown>;
  generated_text: string | null;
  status: "draft" | "sent" | "acknowledged" | "questioned";
  sent_message_id: string | null;
  sent_at: string | null;
  acknowledged_at: string | null;
  authored_by: string | null;
  created_at: string;
  updated_at: string;
};

export type AdherenceSummary = {
  window_days: number;
  total: number;
  taken: number;
  taken_on_time: number;
  taken_late: number;
  missed: number;
  skipped: number;
  delayed: number;
  scheduled: number;
  adherence_rate: number;
  on_time_rate: number;
};

export type LanguageOption = { code: string; label: string };

export type PatientDetail = {
  id: number;
  full_name: string;
  phone: string;
  consent_sms: boolean;
  consent_voice: boolean;
  consent_email: boolean;
  cohort_diabetes: boolean;
  cohort_cardiac: boolean;
  cohort_fall_risk: boolean;
  onboarding_step:
    | "pending"
    | "needs_name"
    | "needs_cohorts"
    | "needs_consent"
    | "done"
    | null;
  preferred_language: string;
  // Ops-initiated bot pause. Non-null timestamp = bot is muted for
  // this patient. Reason / by are informational; the dispatcher gate
  // keys off the timestamp alone.
  bot_paused_at: string | null;
  bot_paused_reason: string | null;
  bot_paused_by: string | null;
  // Right-of-erasure state. Non-null = row PII has been overwritten
  // irreversibly; UI shows the "erased" banner and suppresses live-
  // patient action affordances.
  erased_at: string | null;
  consent_revoked_at: string | null;
  consent_revoked_reason: string | null;
  created_at: string;
  updated_at: string;
  regimens: Regimen[];
  upcoming_appointments: AppointmentSummary[];
  recent_adherence_events: AdherenceEventEntry[];
  recent_refill_events: RefillEventEntry[];
  lab_followups: LabFollowup[];
  adherence_summary: AdherenceSummary;
  recent_side_effect_reports: SideEffectReport[];
};

// Patient timeline — unified chronological view that
// aggregates messages, doses, side-effects, appointments,
// and lab events into a single stream. ``kind`` is the
// discriminator the UI uses to pick a badge / icon. ``detail``
// carries kind-specific extras (entity ids, links).
export type TimelineEventKind =
  | "message_inbound"
  | "message_outbound"
  | "dose_taken"
  | "dose_missed"
  | "dose_skipped"
  | "dose_delayed"
  | "side_effect"
  | "appointment_booked"
  | "appointment_completed"
  | "appointment_cancelled"
  | "appointment_no_show"
  | "lab_booked"
  | "lab_completed"
  | "lab_reviewed"
  | "clinical_alert_high"
  | "clinical_alert_critical";

export type TimelineEvent = {
  occurred_at: string;
  kind: TimelineEventKind;
  title: string;
  body: string | null;
  detail: Record<string, unknown>;
};

export type PatientTimeline = {
  patient_id: number;
  since: string;
  until: string;
  events: TimelineEvent[];
};

// Care plan goals — per-patient quantitative targets
// (HbA1c < 7, BP < 130/80). Distinct from CarePlan
// (cohort-level template). Each goal carries the latest
// observed value + an on-target boolean computed server-side
// so the UI doesn't N+1 on the patient detail page.
export type CarePlanGoalStatus =
  | "active"
  | "achieved"
  | "inactive";
export type CarePlanGoalComparator = "less_than" | "greater_than";
export type MetricObservationSource =
  | "manual"
  | "patient_self_report"
  | "lab"
  | "device";

// Drift classification (slice 17). Computed server-side
// from the last 3 observations + goal target. Drives the
// inline drift badge on the patient detail page; alert-
// worthy values (slipping / persistent_off / stale) also
// trigger ops_tickets via the goal_drift_sweep.
export type GoalDriftStatus =
  | "on_target"
  | "slipping"
  | "persistent_off"
  | "stale"
  | "no_data";

export type CarePlanGoal = {
  id: number;
  patient_id: number;
  metric_key: string;
  metric_label: string;
  target_value: number;
  comparator: CarePlanGoalComparator;
  target_unit: string;
  status: CarePlanGoalStatus;
  ends_on: string | null;
  created_by: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  latest_value: number | null;
  latest_observed_at: string | null;
  on_target: boolean | null;
  drift_status: GoalDriftStatus | null;
};

export type MetricObservation = {
  id: number;
  patient_id: number;
  goal_id: number | null;
  metric_key: string;
  value: number;
  unit: string;
  observed_at: string;
  source: MetricObservationSource;
  recorded_by: string | null;
  notes: string | null;
  created_at: string;
};

// Doctor reply draft — LLM-generated suggestion for an inbox
// row that needs a manual reply. Reviewer always edits + sends
// — this struct is just a starting point. Empty ``draft_text``
// + low confidence + caveat means "AI couldn't help, type
// manually".
export type DraftReply = {
  draft_text: string;
  confidence: "high" | "medium" | "low";
  caveats: string[];
  llm_model: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
};

// Clinical alert — patient-safety signal raised by the
// inbound-message triage classifier. Distinct from the inbox
// (every freeform message gets a row there) and from
// ops_tickets (operational workflow). Alerts are the
// "doctor needs to look at this NOW" subset.
export type ClinicalAlertSeverity = "high" | "critical";
export type ClinicalAlertStatus =
  | "open"
  | "acknowledged"
  | "resolved";

export type ClinicalAlert = {
  id: number;
  patient_id: number;
  patient_phone: string | null;
  message_id: number | null;
  severity: ClinicalAlertSeverity;
  red_flags: string[];
  clinical_summary: string;
  inbound_text: string;
  status: ClinicalAlertStatus;
  llm_model: string | null;
  created_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_notes: string | null;
  // Paging audit. ``paged_doctor_id === null`` after an
  // attempt means "no doctor reachable" — the pager still
  // bumps ``paged_attempts`` so the cap applies.
  paged_doctor_id: number | null;
  paged_at: string | null;
  paged_attempts: number;
};

// Visit brief — LLM-generated pre-visit summary. The doctor
// sees the prose ``summary`` plus action-shaped lists
// (``talking_points`` to surface, ``red_flags`` to flag) and
// ``key_metrics`` numbers above the fold. ``error`` is set on
// failed generations; UI hides those by default but ops admin
// views can include them.
export type VisitBrief = {
  id: number;
  patient_id: number;
  appointment_id: number | null;
  doctor_id: number | null;
  generated_at: string;
  window_start: string;
  window_end: string;
  llm_model: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  summary: string;
  talking_points: string[];
  red_flags: string[];
  key_metrics: {
    adherence_rate?: number | null;
    doses_taken?: number;
    doses_missed?: number;
    doses_total?: number;
    side_effect_count?: number;
    messages_inbound?: number;
    messages_outbound?: number;
  };
  status: "draft" | "sent" | "superseded";
  generated_by: string | null;
  error: string | null;
};

// Broadcast campaigns — cohort bulk send. Each campaign
// materialises into N scheduled events (one per eligible
// recipient). Skipped recipients land on the campaign with
// a reason so ops sees "120 sent, 5 opted-out" up front.
export type BroadcastCampaign = {
  id: number;
  name: string;
  template_name: string;
  template_params: Record<string, string>;
  cohort_filter: Record<string, string>;
  status: "draft" | "materialised" | "completed";
  created_by: string | null;
  created_at: string;
  materialised_at: string | null;
  completed_at: string | null;
  total_recipients: number;
  sent_count: number;
  skipped_count: number;
  notes: string | null;
};

export type BroadcastSend = {
  id: number;
  campaign_id: number;
  patient_id: string;
  patient_db_id: number | null;
  status: "pending" | "skipped" | "dispatched" | "failed";
  skip_reason:
    | "opted_out"
    | "paused"
    | "erased"
    | "no_phone"
    | null;
  scheduled_event_id: number | null;
  created_at: string;
};

export type BroadcastCampaignDetail = {
  campaign: BroadcastCampaign;
  counts_by_status: Record<string, number>;
  counts_by_skip_reason: Record<string, number>;
};

// LLM cost + latency analytics. ``cost_usd_micros`` is integer
// 10⁻⁶ USD so summing across rows stays exact; the UI converts
// to display dollars at render time.
export type LlmCallKindStat = {
  call_kind: string;
  calls: number;
  tokens: number;
  cost_usd_micros: number;
};

export type LlmModelStat = {
  model: string;
  calls: number;
  tokens: number;
  cost_usd_micros: number;
};

export type LlmTopPatientStat = {
  patient_id: string;
  calls: number;
  tokens: number;
  cost_usd_micros: number;
};

export type LlmLatency = {
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  mean_ms: number | null;
};

export type LlmCostAnalytics = {
  since: string;
  until: string;
  total_calls: number;
  total_tokens: number;
  total_cost_usd_micros: number;
  errors_count: number;
  by_call_kind: LlmCallKindStat[];
  by_model: LlmModelStat[];
  top_patients: LlmTopPatientStat[];
  latency: LlmLatency;
};

// Side-effect analytics — clinical pattern detection across the
// panel. ``top_symptoms`` is panel-wide; per-medication breakdowns
// only include reports that mentioned an active regimen
// medication (strict attribution).
export type SideEffectAnalyticsMedication = {
  medication_name: string;
  report_count: number;
  patient_count: number;
  top_symptoms: [string, number][];
};

export type SideEffectAnalyticsCohort = {
  cohort: string;
  report_count: number;
  patient_count: number;
};

export type SideEffectAnalyticsSymptom = {
  symptom: string;
  count: number;
};

export type SideEffectAnalytics = {
  since: string;
  until: string;
  total_reports: number;
  unique_patients: number;
  unique_medications: number;
  by_medication: SideEffectAnalyticsMedication[];
  by_cohort: SideEffectAnalyticsCohort[];
  top_symptoms: SideEffectAnalyticsSymptom[];
};

// Doctor daily digest types — mirror the orchestrator DTO.
export type DigestAppointment = {
  appointment_id: number;
  patient_id: number;
  patient_full_name: string;
  scheduled_for: string;
  end_at: string;
  status: string;
  summary: string | null;
  has_pending_recap: boolean;
};

export type DigestRecapDraft = {
  recap_id: number;
  appointment_id: number;
  patient_id: number;
  patient_full_name: string;
  appointment_date: string;
  created_at: string;
};

export type DigestTicket = {
  ticket_id: number;
  patient_id: number | null;
  patient_full_name: string | null;
  patient_phone: string;
  category: string;
  priority: string;
  status: string;
  sla_minutes: number;
  created_at: string;
  sla_breached_at: string | null;
};

export type DigestSideEffect = {
  ticket_id: number;
  patient_id: number | null;
  patient_full_name: string | null;
  created_at: string;
  status: string;
  reported_text: string | null;
};

export type DoctorDailyDigest = {
  doctor_id: number;
  doctor_name: string;
  when: string;
  summary_counts: Record<string, number>;
  appointments_today: DigestAppointment[];
  recap_drafts_pending: DigestRecapDraft[];
  side_effect_reports_24h: DigestSideEffect[];
  open_tickets: DigestTicket[];
};

// Audit-log search response. ``rows`` is the page; ``total`` is
// the unpaginated count so the UI can render "showing N of M".
export type AuditRecord = {
  id: number;
  record_type: string;
  patient_id: string;
  outbound_mode: string | null;
  flow_action: string | null;
  reason_codes: string[];
  details: Record<string, unknown>;
  logged_at: string;
};

export type AuditSearchResponse = {
  rows: AuditRecord[];
  total: number;
  limit: number;
  offset: number;
};

// One side_effect_report ticket the patient has filed. Surfaced
// on the patient detail page so a doctor can spot patterns across
// multiple events.
export type SideEffectReport = {
  ticket_id: string;
  status: "open" | "acknowledged" | "resolved";
  priority: string;
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  sla_breached_at: string | null;
  // Verbatim "Patient said:" text extracted from the ticket notes
  // by the orchestrator. Null when the ticket was created outside
  // the standard side_effect_handler path (legacy / manual).
  reported_text: string | null;
};

export const orchestrator = {
  dashboard: () => call<Dashboard>(ORCHESTRATOR_URL, "/ops/dashboard"),
  health: () => call<HealthSummary>(ORCHESTRATOR_URL, "/ops/health"),
  analytics: (days = 30) =>
    call<AnalyticsSnapshot>(ORCHESTRATOR_URL, "/ops/analytics", {
      query: { days },
    }),
  listTickets: (
    params: {
      status?: string;
      category?: string;
      assigned_to?: string;
      view?: "active" | "snoozed";
      limit?: number;
    } = {},
  ) =>
    call<OpsTicket[]>(ORCHESTRATOR_URL, "/ops/tickets", { query: params }),
  getTicket: (ticketId: string) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}`),
  ackTicket: (ticketId: string, body: { actor?: string; notes?: string } = {}) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/ack`, {
      method: "POST",
      body,
      signedActor: body.actor,
    }),
  resolveTicket: (ticketId: string, body: { actor?: string; notes?: string } = {}) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/resolve`, {
      method: "POST",
      body,
      signedActor: body.actor,
    }),
  assignTicket: (
    ticketId: string,
    body: { actor?: string; assigned_to?: string | null; notes?: string },
  ) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/assign`, {
      method: "POST",
      body,
    }),
  snoozeTicket: (
    ticketId: string,
    body: {
      actor?: string;
      minutes?: number;
      until?: string;
      notes?: string;
    },
  ) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/snooze`, {
      method: "POST",
      body,
    }),
  unsnoozeTicket: (ticketId: string, body: { actor?: string; notes?: string } = {}) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/unsnooze`, {
      method: "POST",
      body,
    }),
  reopenTicket: (ticketId: string, body: { actor?: string; notes?: string } = {}) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/reopen`, {
      method: "POST",
      body,
    }),
  addTicketNote: (
    ticketId: string,
    body: { actor?: string; note: string },
  ) =>
    call<OpsTicket>(ORCHESTRATOR_URL, `/ops/tickets/${ticketId}/note`, {
      method: "POST",
      body,
    }),
  route: (body: {
    message: {
      message_id: string;
      patient_id: string;
      phone?: string | null;
      text?: string | null;
      input_kind?: "text" | "voice" | "image" | "button";
    };
    last_user_message_at?: string | null;
  }) => call<RouteResponse>(ORCHESTRATOR_URL, "/route", { method: "POST", body }),

  // Doctor-authored reply (clinician → patient, freeform, in-CSW gated)
  sendDoctorReply: (
    patientId: number,
    body: {
      body: string;
      sent_by: string;
      in_reply_to_message_id?: string | null;
    },
  ) =>
    call<{
      status: string;
      wamid: string | null;
      sent_at: string;
      body: string;
      sent_by: string;
    }>(ORCHESTRATOR_URL, `/patients/${patientId}/reply`, {
      method: "POST",
      body,
    }),

  // Patients
  listPatients: () => call<PatientSummary[]>(ORCHESTRATOR_URL, "/patients"),
  getPatient: (patientId: number) =>
    call<PatientDetail>(ORCHESTRATOR_URL, `/patients/${patientId}`),
  getPatientTimeline: (
    patientId: number,
    params: {
      since?: string;
      until?: string;
      kinds?: TimelineEventKind[];
      limit?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.since) search.set("since", params.since);
    if (params.until) search.set("until", params.until);
    if (params.kinds && params.kinds.length > 0)
      search.set("kinds", params.kinds.join(","));
    if (params.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return call<PatientTimeline>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/timeline${qs ? `?${qs}` : ""}`,
    );
  },
  // Visit briefs. ``listVisitBriefs`` skips failed
  // (LLM-errored) rows by default; pass include_failed=true
  // for ops admin views.
  listVisitBriefs: (
    patientId: number,
    params: { limit?: number; include_failed?: boolean } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.limit) search.set("limit", String(params.limit));
    if (params.include_failed)
      search.set("include_failed", "true");
    const qs = search.toString();
    return call<VisitBrief[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/visit-briefs${qs ? `?${qs}` : ""}`,
    );
  },
  getVisitBrief: (briefId: number) =>
    call<VisitBrief>(ORCHESTRATOR_URL, `/visit-briefs/${briefId}`),
  // Clinical alerts. List + per-alert mutations + per-patient
  // sub-list for the patient detail page.
  listClinicalAlerts: (
    params: { status?: ClinicalAlertStatus; limit?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    if (params.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return call<ClinicalAlert[]>(
      ORCHESTRATOR_URL,
      `/clinical-alerts${qs ? `?${qs}` : ""}`,
    );
  },
  clinicalAlertCounts: () =>
    call<Record<string, number>>(
      ORCHESTRATOR_URL,
      "/clinical-alerts/counts",
    ),
  getClinicalAlert: (alertId: number) =>
    call<ClinicalAlert>(
      ORCHESTRATOR_URL,
      `/clinical-alerts/${alertId}`,
    ),
  acknowledgeClinicalAlert: (
    alertId: number,
    body: { actor: string; notes?: string },
  ) =>
    call<ClinicalAlert>(
      ORCHESTRATOR_URL,
      `/clinical-alerts/${alertId}/acknowledge`,
      { method: "POST", body },
    ),
  resolveClinicalAlert: (
    alertId: number,
    body: { actor: string; notes?: string },
  ) =>
    call<ClinicalAlert>(
      ORCHESTRATOR_URL,
      `/clinical-alerts/${alertId}/resolve`,
      { method: "POST", body },
    ),
  pageClinicalAlert: (alertId: number) =>
    call<ClinicalAlert>(
      ORCHESTRATOR_URL,
      `/clinical-alerts/${alertId}/page`,
      { method: "POST", body: {} },
    ),
  listPatientClinicalAlerts: (
    patientId: number,
    params: { limit?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return call<ClinicalAlert[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/clinical-alerts${qs ? `?${qs}` : ""}`,
    );
  },
  generateVisitBrief: (
    patientId: number,
    body: {
      appointment_id?: number;
      doctor_id?: number;
      window_days?: number;
      generated_by?: string;
    } = {},
  ) =>
    call<VisitBrief>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/visit-briefs/generate`,
      { method: "POST", body },
    ),
  resetPatientOnboarding: (patientId: number) =>
    call<PatientDetail>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/reset-onboarding`,
      { method: "POST" },
    ),
  listSupportedLanguages: () =>
    call<LanguageOption[]>(ORCHESTRATOR_URL, "/i18n/languages"),
  updatePatientLanguage: (
    patientId: number,
    body: { preferred_language: string },
  ) =>
    call<PatientDetail>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/preferred-language`,
      { method: "PUT", body },
    ),
  pauseBot: (patientId: number, body: { actor: string; reason: string }) =>
    call<PatientDetail>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/pause-bot`,
      { method: "POST", body, signedActor: body.actor },
    ),
  unpauseBot: (patientId: number, opts: { actor?: string } = {}) =>
    call<PatientDetail>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/unpause-bot`,
      { method: "POST", signedActor: opts.actor },
    ),
  // Right-of-erasure. Irreversible — the UI must guard with a
  // hard-confirm modal before invoking. The endpoint also requires
  // ``confirm: true`` in the body as defense-in-depth.
  erasePatient: (
    patientId: number,
    body: { actor: string; reason: string; confirm: true },
  ) =>
    call<{
      patient_id: number;
      erased_at: string | null;
      anonymized_phone: string;
    }>(ORCHESTRATOR_URL, `/patients/${patientId}/erase`, {
      method: "POST",
      body,
      signedActor: body.actor,
    }),
  // DSAR right-of-access export. Returns the assembled JSON
  // document; the UI converts it to a downloadable Blob locally
  // so the browser triggers a normal download dialog. Signs the actor
  // header (when OPS_ACTOR_SIGNING_KEY is configured) so the
  // operator_actions audit row carries a verified identity.
  exportPatient: (
    patientId: number,
    params: { actor?: string; window_days?: number } = {},
  ) =>
    call<Record<string, unknown>>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/export`,
      { query: params, signedActor: params.actor },
    ),
  // Broadcast campaigns — cohort bulk-send.
  listBroadcastCampaigns: (params: { limit?: number } = {}) =>
    call<BroadcastCampaign[]>(
      ORCHESTRATOR_URL,
      "/campaigns",
      { query: params },
    ),
  getBroadcastCampaign: (campaignId: number) =>
    call<BroadcastCampaignDetail>(
      ORCHESTRATOR_URL,
      `/campaigns/${campaignId}`,
    ),
  listBroadcastRecipients: (
    campaignId: number,
    params: { status?: string; limit?: number; offset?: number } = {},
  ) =>
    call<BroadcastSend[]>(
      ORCHESTRATOR_URL,
      `/campaigns/${campaignId}/recipients`,
      { query: params },
    ),
  createBroadcastCampaign: (body: {
    name: string;
    template_name: string;
    template_params?: Record<string, string>;
    cohort_filter: { cohort: "diabetes" | "cardiac" | "fall_risk" };
    created_by: string;
    notes?: string;
    materialise_immediately?: boolean;
  }) =>
    call<BroadcastCampaignDetail>(
      ORCHESTRATOR_URL,
      "/campaigns",
      { method: "POST", body },
    ),
  // LLM cost + latency analytics — operational telemetry.
  getLlmCostAnalytics: (params: { days?: number } = {}) =>
    call<LlmCostAnalytics>(
      ORCHESTRATOR_URL,
      "/ops/analytics/llm-cost",
      { query: params },
    ),
  // Side-effect frequency analytics — clinical pattern view.
  getSideEffectAnalytics: (params: { days?: number } = {}) =>
    call<SideEffectAnalytics>(
      ORCHESTRATOR_URL,
      "/ops/analytics/side-effects",
      { query: params },
    ),
  // Doctor daily digest — morning panel-management view.
  getDoctorDailyDigest: (
    doctorId: number,
    params: { when?: string } = {},
  ) =>
    call<DoctorDailyDigest>(
      ORCHESTRATOR_URL,
      `/doctors/${doctorId}/daily-digest`,
      { query: params },
    ),
  // Audit log search. Powers the /audit-search ops page —
  // filters mirror the orchestrator endpoint.
  searchAuditLog: (
    params: {
      patient_id?: string;
      record_type?: string;
      reason_code?: string;
      flow_action?: string;
      since?: string;
      until?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    call<AuditSearchResponse>(
      ORCHESTRATOR_URL,
      "/ops/audit-search",
      { query: params },
    ),
  createRegimen: (
    patientId: number,
    body: {
      medication_name: string;
      dose: string;
      schedule: {
        type: "times_of_day";
        times: string[];
        timezone: string;
        frequency: "daily";
      };
      starts_on?: string | null;
      ends_on?: string | null;
      strict_timing?: boolean;
      supply_days_initial?: number | null;
      supply_started_on?: string | null;
    },
  ) =>
    call<Regimen>(ORCHESTRATOR_URL, `/patients/${patientId}/regimens`, {
      method: "POST",
      body,
    }),
  deactivateRegimen: (regimenId: number) =>
    call<Regimen>(ORCHESTRATOR_URL, `/regimens/${regimenId}/deactivate`, {
      method: "POST",
    }),
  fireTestDose: (regimenId: number) =>
    call<{
      scheduled_event_id: number;
      adherence_event_id: number;
      scheduled_for: string;
      note: string;
    }>(ORCHESTRATOR_URL, `/regimens/${regimenId}/test-dose`, {
      method: "POST",
    }),
  fireTestRefill: (regimenId: number) =>
    call<{
      scheduled_event_id: number;
      scheduled_for: string;
      days_left: number | null;
      note: string;
    }>(ORCHESTRATOR_URL, `/regimens/${regimenId}/test-refill`, {
      method: "POST",
    }),

  // Lab follow-ups
  createLabFollowup: (
    patientId: number,
    body: { test_name: string; due_by?: string | null; notes?: string | null },
  ) =>
    call<LabFollowup>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/lab-followups`,
      { method: "POST", body },
    ),
  markLabCompleted: (labId: number) =>
    call<LabFollowup>(
      ORCHESTRATOR_URL,
      `/lab-followups/${labId}/mark-completed`,
      { method: "POST" },
    ),
  markLabReviewed: (labId: number) =>
    call<LabFollowup>(
      ORCHESTRATOR_URL,
      `/lab-followups/${labId}/mark-reviewed`,
      { method: "POST" },
    ),
  fireTestLabReminder: (labId: number) =>
    call<{
      scheduled_event_id: number;
      scheduled_for: string;
      stage: string;
      note: string;
    }>(ORCHESTRATOR_URL, `/lab-followups/${labId}/test-reminder`, {
      method: "POST",
    }),

  // Prescriptions
  listPrescriptions: (status?: "pending" | "verified" | "rejected") =>
    call<Prescription[]>(ORCHESTRATOR_URL, "/prescriptions", {
      query: { status },
    }),
  getPrescription: (prescriptionId: number) =>
    call<Prescription>(
      ORCHESTRATOR_URL,
      `/prescriptions/${prescriptionId}`,
    ),
  verifyPrescription: (
    prescriptionId: number,
    body: {
      verified_by: string;
      regimens: ParsedRegimen[];
      timezone?: string;
    },
  ) =>
    call<Prescription>(
      ORCHESTRATOR_URL,
      `/prescriptions/${prescriptionId}/verify`,
      { method: "POST", body },
    ),
  rejectPrescription: (
    prescriptionId: number,
    body: { rejected_by: string; reason?: string },
  ) =>
    call<Prescription>(
      ORCHESTRATOR_URL,
      `/prescriptions/${prescriptionId}/reject`,
      { method: "POST", body },
    ),

  listDoctors: () => call<Doctor[]>(ORCHESTRATOR_URL, "/doctors"),
  createDoctor: (body: {
    name: string;
    email: string;
    phone?: string | null;
    timezone?: string;
    calendar_id?: string;
  }) => call<Doctor>(ORCHESTRATOR_URL, "/doctors", { method: "POST", body }),
  setDoctorOnCall: (doctorId: number, on_call: boolean) =>
    call<Doctor>(
      ORCHESTRATOR_URL,
      `/doctors/${doctorId}/on-call`,
      { method: "POST", body: { on_call } },
    ),
  disconnectDoctor: (doctorId: number) =>
    call<Doctor>(ORCHESTRATOR_URL, `/doctors/${doctorId}/disconnect`, {
      method: "POST",
    }),
  doctorAvailability: (
    doctorId: number,
    args: { start: string; end: string; duration_minutes?: number },
  ) =>
    call<DoctorAvailability>(
      ORCHESTRATOR_URL,
      `/doctors/${doctorId}/availability`,
      { query: args },
    ),

  // Care plans (cohort standing orders)
  listCarePlans: (params: { include_inactive?: boolean } = {}) =>
    call<CarePlan[]>(ORCHESTRATOR_URL, "/care-plans", {
      query: { include_inactive: params.include_inactive ? "1" : undefined },
    }),
  listCarePlanCohorts: () =>
    call<CarePlanCohortOption[]>(ORCHESTRATOR_URL, "/care-plans/cohorts"),
  createCarePlan: (body: {
    cohort_attr?: string | null;
    cohort_tag_id?: number | null;
    test_name: string;
    cadence_days: number;
    due_in_days?: number;
    notes?: string | null;
    created_by?: string | null;
  }) =>
    call<CarePlan>(ORCHESTRATOR_URL, "/care-plans", {
      method: "POST",
      body,
    }),
  updateCarePlan: (
    planId: number,
    body: {
      cadence_days?: number;
      due_in_days?: number;
      active?: boolean;
      notes?: string | null;
    },
  ) =>
    call<CarePlan>(ORCHESTRATOR_URL, `/care-plans/${planId}`, {
      method: "PUT",
      body,
    }),
  deactivateCarePlan: (planId: number) =>
    call<CarePlan>(ORCHESTRATOR_URL, `/care-plans/${planId}/deactivate`, {
      method: "POST",
    }),

  // Doctor inbox (classified inbound messages)
  listInbox: (
    params: {
      category?: string;
      urgency?: string;
      escalated?: boolean;
      patient_phone?: string;
      input_kind?: string;
      limit?: number;
    } = {},
  ) => {
    const query: Record<string, string | number | undefined> = {};
    if (params.category) query.category = params.category;
    if (params.urgency) query.urgency = params.urgency;
    if (params.escalated !== undefined) {
      query.escalated = params.escalated ? "true" : "false";
    }
    if (params.patient_phone) query.patient_phone = params.patient_phone;
    if (params.input_kind) query.input_kind = params.input_kind;
    if (params.limit !== undefined) query.limit = params.limit;
    return call<InboundClassification[]>(ORCHESTRATOR_URL, "/ops/inbox", {
      query,
    });
  },
  inboxCategoryCounts: (days = 7) =>
    call<Record<string, number>>(
      ORCHESTRATOR_URL,
      "/ops/inbox/category-counts",
      { query: { days } },
    ),
  // Bot-reply quality feedback on a single inbox row.
  setInboxFeedback: (
    classificationId: number,
    body: { rating: -1 | 1; actor: string; note?: string },
  ) =>
    call<InboundClassification>(
      ORCHESTRATOR_URL,
      `/ops/inbox/${classificationId}/feedback`,
      { method: "POST", body },
    ),
  clearInboxFeedback: (classificationId: number) =>
    call<InboundClassification>(
      ORCHESTRATOR_URL,
      `/ops/inbox/${classificationId}/feedback`,
      { method: "DELETE" },
    ),
  // Care plan goals — per-patient quantitative tracking.
  listPatientGoals: (
    patientId: number,
    params: { status?: CarePlanGoalStatus } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    const qs = search.toString();
    return call<CarePlanGoal[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/goals${qs ? `?${qs}` : ""}`,
    );
  },
  createPatientGoal: (
    patientId: number,
    body: {
      metric_key: string;
      metric_label: string;
      target_value: number;
      comparator?: CarePlanGoalComparator;
      target_unit: string;
      ends_on?: string | null;
      created_by?: string | null;
      notes?: string | null;
    },
  ) =>
    call<CarePlanGoal>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/goals`,
      { method: "POST", body },
    ),
  updatePatientGoalStatus: (
    patientId: number,
    goalId: number,
    status: CarePlanGoalStatus,
  ) =>
    call<CarePlanGoal>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/goals/${goalId}/status`,
      { method: "PATCH", body: { status } },
    ),
  recordGoalObservation: (
    patientId: number,
    goalId: number,
    body: {
      value: number;
      unit: string;
      observed_at?: string;
      source?: MetricObservationSource;
      recorded_by?: string | null;
      notes?: string | null;
    },
  ) =>
    call<MetricObservation>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/goals/${goalId}/observations`,
      { method: "POST", body },
    ),
  listGoalObservations: (
    patientId: number,
    goalId: number,
    params: { limit?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return call<MetricObservation[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/goals/${goalId}/observations${qs ? `?${qs}` : ""}`,
    );
  },
  draftInboxReply: (classificationId: number) =>
    call<DraftReply>(
      ORCHESTRATOR_URL,
      `/ops/inbox/${classificationId}/draft-reply`,
      { method: "POST", body: {} },
    ),

  // Cohort tags (clinician-authored cohort labels)
  listCohortTags: (params: { include_inactive?: boolean } = {}) =>
    call<CohortTag[]>(ORCHESTRATOR_URL, "/cohort-tags", {
      query: { include_inactive: params.include_inactive ? "1" : undefined },
    }),
  createCohortTag: (body: {
    label: string;
    slug?: string | null;
    description?: string | null;
    created_by?: string | null;
  }) =>
    call<CohortTag>(ORCHESTRATOR_URL, "/cohort-tags", {
      method: "POST",
      body,
    }),
  updateCohortTag: (
    tagId: number,
    body: {
      label?: string;
      description?: string | null;
      active?: boolean;
    },
  ) =>
    call<CohortTag>(ORCHESTRATOR_URL, `/cohort-tags/${tagId}`, {
      method: "PUT",
      body,
    }),
  listPatientCohortTags: (patientId: number) =>
    call<PatientCohortTagAssignment[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/cohort-tags`,
    ),
  assignPatientCohortTag: (
    patientId: number,
    body: { cohort_tag_id: number; assigned_by?: string | null },
  ) =>
    call<PatientCohortTagAssignment>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/cohort-tags`,
      { method: "POST", body },
    ),
  removePatientCohortTag: (patientId: number, tagId: number) =>
    call<null>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/cohort-tags/${tagId}`,
      { method: "DELETE" },
    ),

  // Caregivers (cc on recaps + future channels)
  listCaregivers: (
    patientId: number,
    params: { include_inactive?: boolean } = {},
  ) =>
    call<Caregiver[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/caregivers`,
      {
        query: {
          include_inactive: params.include_inactive ? "1" : undefined,
        },
      },
    ),
  createCaregiver: (
    patientId: number,
    body: {
      full_name: string;
      phone: string;
      relationship_to_patient?: string | null;
      notify_on_recap?: boolean;
    },
  ) =>
    call<Caregiver>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/caregivers`,
      { method: "POST", body },
    ),
  updateCaregiver: (
    caregiverId: number,
    body: {
      full_name?: string;
      relationship_to_patient?: string | null;
      notify_on_recap?: boolean;
      active?: boolean;
    },
  ) =>
    call<Caregiver>(
      ORCHESTRATOR_URL,
      `/caregivers/${caregiverId}`,
      { method: "PUT", body },
    ),
  confirmCaregiverConsent: (
    caregiverId: number,
    body: { confirmed_by: string },
  ) =>
    call<Caregiver>(
      ORCHESTRATOR_URL,
      `/caregivers/${caregiverId}/confirm-consent`,
      { method: "POST", body },
    ),
  revokeCaregiverConsent: (caregiverId: number) =>
    call<Caregiver>(
      ORCHESTRATOR_URL,
      `/caregivers/${caregiverId}/revoke-consent`,
      { method: "POST" },
    ),
  sendCaregiverConsentPrompt: (caregiverId: number) =>
    call<{
      status: string;
      wamid: string | null;
      sent_at: string;
      caregiver_id: number;
      template_name: string;
    }>(
      ORCHESTRATOR_URL,
      `/caregivers/${caregiverId}/send-consent-prompt`,
      { method: "POST" },
    ),

  // Care plan exemptions (patient-level opt-outs)
  listPatientExemptions: (
    patientId: number,
    params: { include_inactive?: boolean } = {},
  ) =>
    call<CarePlanExemption[]>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/care-plan-exemptions`,
      {
        query: {
          include_inactive: params.include_inactive ? "1" : undefined,
        },
      },
    ),
  createPatientExemption: (
    patientId: number,
    body: {
      care_plan_id: number;
      reason: string;
      expires_at?: string | null;
      created_by?: string | null;
    },
  ) =>
    call<CarePlanExemption>(
      ORCHESTRATOR_URL,
      `/patients/${patientId}/care-plan-exemptions`,
      { method: "POST", body },
    ),
  revokePatientExemption: (
    exemptionId: number,
    body: { revoked_by?: string | null } = {},
  ) =>
    call<CarePlanExemption>(
      ORCHESTRATOR_URL,
      `/care-plan-exemptions/${exemptionId}/revoke`,
      { method: "POST", body },
    ),

  // Appointments + recap
  getAppointment: (appointmentId: number) =>
    call<AppointmentDetail>(
      ORCHESTRATOR_URL,
      `/appointments/${appointmentId}`,
    ),
  getPreVisitSummary: (appointmentId: number) =>
    call<PreVisitSummary>(
      ORCHESTRATOR_URL,
      `/appointments/${appointmentId}/pre-visit`,
    ),
  getAppointmentRecap: (appointmentId: number) =>
    call<AppointmentRecap | null>(
      ORCHESTRATOR_URL,
      `/appointments/${appointmentId}/recap`,
    ),
  upsertAppointmentRecap: (
    appointmentId: number,
    body: {
      doctor_notes?: string | null;
      structured: RecapStructuredPayload;
      authored_by?: string | null;
    },
  ) =>
    call<AppointmentRecap>(
      ORCHESTRATOR_URL,
      `/appointments/${appointmentId}/recap`,
      { method: "PUT", body },
    ),
  previewAppointmentRecap: (appointmentId: number) =>
    call<{ body: string }>(
      ORCHESTRATOR_URL,
      `/appointments/${appointmentId}/recap/preview`,
      { method: "POST" },
    ),
  sendAppointmentRecap: (appointmentId: number) =>
    call<AppointmentRecap>(
      ORCHESTRATOR_URL,
      `/appointments/${appointmentId}/recap/send`,
      { method: "POST" },
    ),
};

// ---- Scheduler -------------------------------------------------------------

export type ScheduledEvent = {
  id: number;
  event_type: string;
  patient_id: string;
  scheduled_for: string;
  payload: Record<string, unknown>;
  status: "pending" | "dispatched" | "skipped" | "failed";
  created_at: string;
  dispatched_at: string | null;
  error: string | null;
};

export const scheduler = {
  listEvents: (params: { status?: string; patient_id?: string; limit?: number } = {}) =>
    call<ScheduledEvent[]>(SCHEDULER_URL, "/scheduled-events", { query: params }),
  tick: () => call<{ examined: number; dispatched: number; failed: number; skipped: number }>(
    SCHEDULER_URL,
    "/scheduler/tick",
    { method: "POST" },
  ),
};

// ---- Gateway ---------------------------------------------------------------

export type LogEntry =
  | {
      direction: "inbound";
      received_at: string;
      message: Record<string, unknown>;
    }
  | {
      direction: "outbound";
      sent_at: string;
      payload_type: "template" | "freeform" | null;
      message: Record<string, unknown>;
    };

export type MessageOutBody = {
  patient_id?: string | null;
  phone?: string | null;
  body?: string | null;
  use_template?: boolean;
  template_name?: string | null;
  template_params?: Record<string, string | number | boolean>;
  quick_replies?: { id: string; title: string }[];
  buttons?: { id: string; label: string; action: string }[];
};

export type GatewaySendResult = {
  status: string;
  payload_type: "template" | "freeform";
  dry_run: boolean;
  wamid: string | null;
};

export const gateway = {
  logs: (limit = 100) =>
    call<LogEntry[]>(GATEWAY_URL, "/logs", { query: { limit } }),
  send: (message: MessageOutBody) =>
    call<GatewaySendResult>(GATEWAY_URL, "/send", { method: "POST", body: message }),
};

export const backendUrls = { ORCHESTRATOR_URL, SCHEDULER_URL, GATEWAY_URL };
