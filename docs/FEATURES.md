# Medagent — Features

_Comprehensive catalog of what's built and live in the medagent platform.
Companion to [`source_of_truth.md`](source_of_truth.md) (product vision) and
[`ROADMAP.md`](ROADMAP.md) (gap tracker / progress log)._

_Last verified: 2026-05-27 — full unit + integration suites green; all 4
services live in production with HMAC-signed operator identity enforced._

---

## At a glance

| | |
|---|---|
| **Services** | `orchestrator` (FastAPI/LangGraph), `whatsapp_gateway` (FastAPI), `scheduler` (FastAPI background sweeps), `ops_console` (Next.js 16 / React 19) |
| **Inbound channels** | WhatsApp Cloud API (text + voice), Google Calendar push, Meta delivery-status callbacks |
| **Storage** | Postgres (Supabase) via SQLAlchemy 2 async; Alembic migrations at head `0050` |
| **LLM** | OpenAI (env-gated; deterministic rule-based fallback; per-call cost tracking; circuit-breaker on degraded upstream) |
| **Background workers** | ~21 sweep modules (dose / refill / lab / recap / care-gap / delivery / SLA / pregnancy / postpartum / asthma / on-call re-page / weekly-trend / etc.) — each guarded by a per-sweep circuit breaker |
| **API surface** | ~30 domain routers (`services/orchestrator/routers/`) + a thin `/route` dispatcher |
| **Tests** | 654 Python unit + ~560 integration (xdist parallel; serial-marker for global-state sweeps) + 20 ops-console vitest |
| **CI** | GitHub Actions on every PR + push: gitleaks secret scan, ruff, unit suite, ops-console lint + vitest + Next.js build. Branch protection on `main`. |
| **Security** | Fail-closed service auth (shared secret), HMAC-signed `X-Ops-Actor` (enforced in prod), per-operator audit log, CSP + security headers, PII log redaction, Fernet at-rest OAuth-token encryption |
| **Deploy** | Coolify on a VPS — Dockerfile builds, Traefik + Let's Encrypt TLS, GitHub App auto-deploy on push to `main` |

---

## 1. Patient-facing (WhatsApp)

### Onboarding & consent
- Care opt-in template; per-patient `preferred_language` (i18n)
- Onboarding reset endpoint for ops re-flow
- Multi-patient household model: one caregiver → many patients

### Medication adherence
- Dose reminders (utility templates out-of-CSW; freeform in-CSW)
- Quick replies: **Taken / Snooze / Skip**
- Late "Mark as taken" after a sweep-marked miss (`late_confirmed` metadata)
- Missed-dose **reason capture** (`FORGOT` / `SIDE_EFFECT` / `OUT_OF_STOCK` / `CONFUSED` / `COST` / `OTHER`) → recovery routing
- Consecutive-miss → idempotent escalation `ops_ticket`
- 30-day adherence summary (taken / on-time / late / missed / skipped + rate)

### Refills
- `Regimen.supply_days_initial` + `supply_started_on` → days-of-supply forecast
- T-7 / T-3 / T-1 reminder ladder (`refill_due_v1`)
- **"Reorder"** action via opt-in button (`REFILL_REORDER_ENABLED`) — creates an `Order` via the pluggable `PharmacyAdapter` (default: deep-link)
- Substitution proposal → patient Approve / Decline (`substitution_approval_v1`)
- Order lifecycle: `pending → confirmed → … → delivered` + delivery receipt (`order_receipt_v1`)

### Labs & appointments
- Lab follow-ups: `due → booked → completed → reviewed` (`lab_due_v1`, `lab_closure_update_v1`)
- Appointment booking against doctor's **Google Calendar** (availability, create, cancel) — encrypted refresh tokens
- Appointment reminders T-24h / T-1h (status-aware; cancellation cancels reminders)
- Post-visit **recap** (LLM-polished, doctor-edited; ack / question quick-reply buttons; locks after send)

### Vitals & cohort packs
- Self-report parser: **glucose, BP (systolic/diastolic pair), weight, HbA1c, peak flow** (text and voice) → `metric_observation` + links to active `care_plan_goal`
- **Weekly trend** push (`weekly_trend_v1`, idempotent dedupe per patient)
- **Asthma** pack: rescue-inhaler puff counting + rolling-7d poor-control detector (≥8 puffs OR ≥3 days) → idempotent `asthma_control` ticket; trigger diary
- **Pregnancy** pack: LMP/EDD intake, Naegele gestational-week math, 17-entry ANC milestone schedule, weekly check-ins (`pregnancy_weekly_v1`), end-pregnancy cancels pending events
- **Postpartum** pack: end-pregnancy with `birth_outcome=delivered` + delivery date auto-transitions to a 12-week postpartum phase; EPDS-anchored milestone schedule (early visit, day 6-8 visit, EPDS mental-health screens at D14 + 6wk, 6-week visit + contraception counsel, 8-week baby vaccines, 12-week close) + weekly check-ins (`postpartum_check_v1`); non-delivered outcomes skip the cadence
- **Post-op antibiotics**: strict timed reminders + day-N checklist (`post_op_check_v1`); wound-photo → `wound_review` ops ticket
- **Senior care**: household model + caregiver mode + simplified action set; **caregiver dose-reminder fan-out** (opt-in `notify_on_dose_reminder` + `CAREGIVER_DOSE_FANOUT_ENABLED` gate) — caregivers receive parallel dose reminders + can mark Taken/Skipped on behalf of the patient (attributed in `confirmation_metadata`)

### Voice notes
- Inbound `[voice-note]` marker → **faster-whisper** local CPU/int8 transcription (optional `voice` extra; graceful fallback if absent)
- Transcribed once at `/route` entry → mutates `message.text` so routing, vitals parsing, inbox classification, clinical triage, and `message_log` all see the transcript

### Safety & escalation
- **Clinical red-flag triage** (LLM + rule-based) → `clinical_alert` row + `clinical_alert_page` event
- **On-call paging**: rota fanout with re-page sweep (`clinical_alert_repage_sweep`); excludes last-paged doctor on re-page
- LLM **safety augmentation** cross-check before reply composition
- Side-effect report capture (verbatim "Patient said:" block preserved → analytics)
- Every reply exposes a human escalation path (CALL / HELP buttons)

### Privacy (compliance)
- **Right-to-erasure** (GDPR Art. 17 / India DPDP §13): scrubs PII across **17 linked tables** (patient, caregivers, message_log, inbound classifications, clinical alerts, visit briefs, broadcast snapshots, metric obs, care-plan goals, pregnancies, orders, post-op, asthma triggers, households, appointment recaps, lab followups, prescriptions)
- **DSAR right-of-access export**: single JSON document of patient data (regimens, adherence, labs, recaps, cohort tags, exemptions, side-effect reports, etc.) + audit row
- **HMAC-signed operator attribution**: `X-Ops-Actor` header carries an HMAC-SHA256 signature (`X-Ops-Actor-Signature`); orchestrator verifies it on all 7 privileged endpoints. Enforced in prod (`OPS_ACTOR_SIGNATURE_REQUIRED=1`) — an API-key holder without the signing key cannot forge operator identity
- **Per-operator audit log** (`operator_actions`): every privileged action (DSAR export, erasure, pause/unpause, ticket ack/resolve, exemption grant/revoke) writes a row keyed by `(operator_id, action, target)` with a `signed` trust flag
- **PII log redaction**: phone numbers masked in operational logs (`redact_phone`)
- Ops-initiated **bot pause** + consent revocation (dispatcher gate)

---

## 2. Doctor-facing

### Inbox & reply
- **Doctor inbox**: triaged inbound feed with `category` / `urgency` / `handler_used` / `escalated` flags
- **AI-drafted reply** (LLM) — pre-fills the textarea with confidence + caveats banner
- Doctor-authored freeform reply (in-CSW), routed to the patient + linked back to the inbound classification
- Per-row **thumbs-up / thumbs-down** feedback (quality signal, surfaced in analytics)

### Daily digest
- Per-doctor end-of-day digest: misses, side-effect reports, ack queue, recap-awaiting state
- **Adherence-pattern alerts**: week-over-week delta detection (regression → ticket)

### Pre-visit brief
- One-call aggregate for the doctor before an appointment: appointment + patient summary + cohort flags/tags + active regimens + 30-day adherence + open lab followups + open ops tickets + active care-plan exemptions + recent inbox + prior visit's recap excerpt + caregiver-cc indicator

### Visit recap
- Doctor fills `structured_payload` (`meds_added/changed/stopped`, `labs_ordered`, `next_followup_in_days`, `red_flags`) + free-form `doctor_notes`
- LLM polishes → `generated_text` → sent to patient (+ caregiver cc)
- v1 / v2 templates; v2 carries dynamic `recap-ack-{id}` / `recap-question-{id}` buttons that route back to `recap_handler`
- Send locks the recap (PUT → 409)
- **Ack-nudge sweep** (idempotent enqueue) for unacknowledged recaps

### Calendar
- Two-way Google Calendar sync per doctor — OAuth (encrypted refresh token via Fernet), push webhook (`/webhooks/google-calendar`) + polling backstop
- On-call toggle + rota fanout for critical pages

### Visit briefs (LLM)
- Manual + auto-T-2h LLM-generated visit briefs with token-cost tracking

### Care plans & goals
- Standing-order **care plans** per cohort (e.g., HbA1c every 180 days for `cohort_diabetes`)
- Per-patient quantitative **goals** (e.g., HbA1c < 7.0) + observation logging + drift detection
- **Goal-drift sweep** → ops ticket; **auto-achievement** on N consecutive on-target observations
- Per-patient **care-plan exemptions** (audited, expirable, reason-required)

---

## 3. Operator / clinic-ops (Next.js ops console at `app.ramkaaj.com`)

### Patient management
- Patients list with cohort flags + ticket counts (single grouped query — no N+1)
- Patient detail aggregate (regimens / appointments / adherence / refills / labs / cohort tags / exemptions / side-effect history)
- Patient **timeline** (unified per-patient event stream)
- Language switch, onboarding reset, bot pause/unpause
- **Right-to-erasure** trigger UI: typed-name confirmation + reason + acknowledgement checkbox
- **DSAR export**: client-side JSON download (server action returns the document; Blob built in the browser)

### Ticket queue
- Full CRUD + lifecycle: `open / acknowledged / resolved / snoozed / unsnoozed / reopened`
- Notes append (audited per-actor); assignment
- **SLA breach sweep** stamps `sla_breached_at` (persistent first-cross marker)
- Filters: category / priority / assignee / snoozed / active-only / status

### Cohort + program ops
- **Cohort tags**: clinician-authored labels — assign / remove / list per patient
- **Care plan exemptions**: per-patient opt-outs with expiry + reason + audit
- **Broadcast campaigns**: any_of / all_of composite cohort filters; tag + flag targeting (6 cohorts: diabetes / cardiac / fall_risk / asthma / post_op / pregnancy); materializer + per-send delivery stats
- **Visit briefs** management (manual generate + scheduled list)

### Analytics dashboards
- **Program metrics** (`/ops/dashboard`): adherence rate, refill-risk rate, follow-up closure, recap funnel, open tickets, regimens-low, missed-dose escalations, refill-help, labs overdue, lab-help, prescriptions pending, SLA-overdue, **care-gaps** (batched per-plan — no N+1), scheduled-events **DLQ**
- **Recap funnel** (sent vs acknowledged vs questioned; drafts excluded)
- **Delivery rollup** (24h delivery vs failure; per-template breakdown)
- **LLM cost + latency** analytics (per model, per handler)
- **Side-effect analytics**: history per patient + cohort-level
- **Doctor reply feedback** analytics

### Inbox
- Triaged inbound feed with classification + urgency + handler trace
- AI-draft reply form (per-row) + send (in-CSW)
- Per-row good/bad feedback

### Audit + observability
- `/audit-search` across the workflow audit log
- **Service-health reconciler**: detects stale + consecutive-error components → idempotent ticket
- **DLQ** list + manual retry endpoint for permanently-failed scheduled events
- Per-template **delivery alerting** sweep

### Doctor management
- Doctor list, on-call toggle, Google OAuth connect (start + callback), digest preview

---

## 4. Platform / reliability / security

### Inbound `/route` dispatcher
- **Inbound replay dedupe**: `processed_inbound_messages` ledger w/ ON CONFLICT atomic claim — Meta webhook redeliveries short-circuit to a no-op (no re-run, no re-send, no LLM re-charge)
- Voice-note transcription before any routing
- **Per-patient rate limit** gate (fires before any LLM / handler / DB write)
- LangGraph compiled **StateGraph** with Postgres **checkpointer** (per-patient `thread_id`, durable conversation state) + deterministic sync-fallback runner when LangGraph is disabled
- **24-hour customer-service-window** policy gate (free-form vs template, with reason codes)
- LLM intent classification + safety augmentation; rule-based fallback on any LLM failure
- Cohort-aware **triage** (diabetes / cardiac / asthma / pregnancy / post-op / fall-risk)
- Doctor-inbox annotation + clinical-alert paging (best-effort, never blocks the response)

### Scheduler dispatch tick
- **Per-event commit** (no rollback of already-sent messages on a mid-batch error)
- **4xx permanent gateway failures → DLQ immediately** (don't burn the retry budget)
- 5xx / network / 408 / 429 → bounded exponential backoff + jitter (default 5 attempts, ~5h horizon)
- **Idempotent enqueue** primitive (ON CONFLICT on `idempotency_key`) — used by recap ack-nudge, weekly trend, all materializers
- **Per-row SAVEPOINT isolation** in the missed-dose sweep (one bad row can't poison the pass)
- **Per-sweep circuit breaker** (`app/circuit_breaker.py`): a sweep that crashes `SWEEP_BREAKER_THRESHOLD` (5) consecutive ticks opens its breaker for `SWEEP_BREAKER_RESET_SECONDS` (600), heartbeats `circuit_open` (surfaced by the service-health reconciler), then half-opens for a probe. Prevents a deterministically-broken sweep from spamming logs / DLQ for hours

### Sweep workers (`services/scheduler/*`)
`adherence_pattern_sweep`, `appointment_reminders`, `calendar_sync_sweep`, `care_gaps`, `clinical_alert_repage_sweep`, `delivery_alert_sweep`, `delivery_template_alert_sweep`, `dose_reminders`, `goal_drift_sweep`, `lab_followups`, `missed_doses`, `post_op_checklist`, `postpartum_milestones`, `pregnancy_milestones`, `recap_sweeps`, `refill_reminders`, `service_health_reconciler`, `sla_breach_sweep`, `visit_brief_scheduling`, `weekly_trend_sweep`.

### WhatsApp gateway
- **Meta send retry/backoff** in `_post_to_meta` for transient failures (3 attempts, jittered exponential)
- Dry-run mode (`WHATSAPP_DRY_RUN=1`) — logs outbound to `message_log` without hitting Meta
- Inbound webhook (internal-only; production webhook lives in the Next.js ops console)
- Status callback ingest (`whatsapp_message_statuses` — delivered / read / failed) — POSTed by the ops-console webhook handler

### Auth & security
- **Fail-closed startup** on orchestrator + gateway: refuse to boot without `ORCHESTRATOR_API_KEY` / `GATEWAY_API_KEY` unless `ALLOW_UNAUTHENTICATED=1` (dev/test opt-in)
- Shared-secret middleware (`X-API-Key` or `Authorization: Bearer`) on every endpoint except `/health` (exact) and `/webhooks/` (prefix; Meta verify-token guarded inside the handler)
- **HMAC-signed operator identity** (`app/operator_signature.py`): the ops console signs `X-Ops-Actor` with `OPS_ACTOR_SIGNING_KEY`; the orchestrator's `resolve_actor` verifies it on all 7 privileged endpoints. `OPS_ACTOR_SIGNATURE_REQUIRED=1` (set in prod) → 401 on unsigned / mis-signed. Two-stage rollout (observe → enforce) is complete
- **Per-operator audit** (`operator_actions` table) — durable, queryable by `(operator_id, logged_at)` and `(target_type, target_id)`
- **WhatsApp webhook signature verification** — `X-Hub-Signature-256` HMAC checked against `WHATSAPP_APP_SECRET` before any processing (ops-console webhook)
- **CSP + security headers** (ops_console `next.config.ts`): Content-Security-Policy, HSTS, X-Frame-Options DENY, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- ops_console **`server-only` boundary** on `backend.ts` (Client Components route through Server Actions; build fails if a client component imports the secret-reading module)
- ops_console `call()` has a 30s `AbortSignal.timeout` + guarded JSON parsing
- **PII log redaction** (`app/logging_redact.py`) — phone numbers masked to last-4 in logs
- Fernet at-rest encryption for OAuth refresh tokens
- **Secret scanning** (gitleaks) + pre-commit hooks block `.env`-style leaks

### Data
- Postgres (Supabase) — single shared DB; Alembic chain at head **`0050`**
- Composite indexes: `ops_tickets(patient_id, category, status)`, `clinical_alerts(patient_id, status)`, `metric_observations(goal_id, observed_at DESC)` for hot lookups
- `processed_inbound_messages` dedupe ledger; `operator_actions` audit log
- DB pool `10 + 10` per process (under Supabase's 30-conn cap)
- Patient-by-phone lookups memoized per session (collapses the 4-7 duplicate fetches per inbound to 1)
- Right-of-erasure spans 17 PII-bearing tables (incl. AppointmentRecap / LabFollowup / Prescription closed by the latest pass)
- Every workflow decision writes an audit row (`audit_records`) with reason codes + actor

### LLM
- OpenAI client gated by `LLM_ENABLED` + `OPENAI_API_KEY`; configurable model + timeout
- Per-call cost + latency logging in `llm_call_logs` (attributed to patient + message_id via contextvar)
- **Circuit breaker** — `LLM_BREAKER_THRESHOLD` (5) consecutive failures open the breaker for `LLM_BREAKER_RESET_SECONDS` (120); `_get_client()` returns `None` while open so every caller falls back to the deterministic path instead of paying the timeout
- Surfaced as the LLM analytics dashboard

### Test harness
- 654 Python unit + ~560 integration — both fully green; 20 ops-console vitest
- `pytest-xdist` parallel runs (`pytest -n auto --dist loadgroup`); `@pytest.mark.serial` for global-state sweeps
- `conftest` defaults: `WHATSAPP_DRY_RUN=1`, `LLM_ENABLED=0`, `LANGGRAPH_ENABLED=0`, `ALLOW_UNAUTHENTICATED=1`, prod API keys cleared
- `MEDAGENT_TEST_CLEANUP=1` clears accumulated shared-DB test rows between runs
- **CI** (`.github/workflows/ci.yml`): gitleaks + ruff + unit suite + ops-console lint/vitest/build on every PR + push

---

## 5. Deployment (production)

- Production Dockerfiles in repo root (Python services via `SERVICE=` env selector) and `apps/ops_console/Dockerfile` (Next.js 16 multi-stage)
- Live on Coolify (`72.62.241.119`) — project `medagent`:
  - Orchestrator → `https://api.ramkaaj.com`
  - Ops console → `https://app.ramkaaj.com`
  - Gateway + scheduler → internal sslip URLs (server-to-server only)
- Traefik + Let's Encrypt TLS, GitHub App auto-deploys on push to `main`
- WhatsApp webhook configured at `https://app.ramkaaj.com/api/whatsapp/webhook`; Google OAuth redirect at `https://app.ramkaaj.com/api/google/oauth/callback`
- Prod env: `OPS_ACTOR_SIGNING_KEY` + `OPS_ACTOR_SIGNATURE_REQUIRED=1` set on orchestrator + ops console

---

## 6. Currently deferred (per SoT §11)

- In-chat payments
- ABDM / ABHA integration
- Autonomous clinical diagnosis / treatment

---

_For product vision and target cohorts, see [`source_of_truth.md`](source_of_truth.md). For the live progress log and what's in flight, see [`ROADMAP.md`](ROADMAP.md)._
