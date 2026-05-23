# Medagent Roadmap & Gap Tracker

_Living document. Tracks the gap between the [Source of Truth](source_of_truth.md)
product vision and the current implementation. Updated as items land._

_Created: 2026-05-10 · Source: vision-vs-implementation audit_

## Legend
- ✅ **Done** — built, tested, in main
- 🟡 **Partial** — some plumbing exists, key path missing
- 🔴 **Not started** — no implementation
- ⏸️ **Deferred (non-goal)** — intentionally out of current scope per SoT §11

---

## Already shipped (context)

The operational backbone is complete and on `main`:
- **SoT Sprints 1–5**: miss-recovery + reason capture, refill stage ladder,
  cohort triage packs, caregiver digest, SLA escalation, lab/appointment
  closure loops, ops console, durable persistence (Alembic 0001–0037).
- **V2 "doctors love it" layer**: doctor daily digest, adherence pattern
  alerts, side-effect analytics, calendar two-way sync, LLM cost/latency
  tracking, cohort broadcast campaigns, patient timeline, LLM visit briefs
  (manual + auto T-2h), clinical red-flag triage + on-call paging, care-plan
  goal tracking + drift alerting, doctor reply drafter.
- **V3 hardening**: right-of-erasure across all PII tables, TRIAGE_ENABLED
  flag, test-infra pool/cleanup/loop fixes.
- **Tooling**: graphify knowledge graph (`graphify-out/`).

---

## Open items (priority order)

### P1 — ✅ Refill emit/dispatch contract bug  · _task #7 — DONE_
A real defect, not just test debt. `/emit-refill-due` (`RefillDueRequest`)
doesn't accept `regimen_id`, but `services/scheduler/dispatcher.py` raises
`"refill payload missing regimen_id"` and skips the event — so **refill
reminders never dispatch**. Hidden behind a skipped test
(`test_scheduler.py::test_tick_dispatches_due_events_and_marks_dispatched`).
- [ ] Thread `regimen_id` through request → scheduled_event payload → dispatcher
- [ ] Unskip the test
- **SoT ref:** MVP #4 (refill forecasting), Epic 4

### P2 — ✅ Patient vitals self-report inbound flow  · _task #8 — DONE_
The SoT's daily-retention engine. Patients text `"sugar 140"` / `"BP 130/85"`
and get weekly trends. Storage exists (slice 14 `metric_observations` with a
`patient_self_report` source value) but **no inbound flow writes it** — the
enum value has nothing producing it.
- [ ] Inbound handler: parse reading from message → `metric_observation`
- [ ] Match to active `care_plan_goal` by metric
- [ ] Weekly trend summary surfaced to patient
- **SoT ref:** §3A/§3B (glucose/BP capture + trend), MVP #1

### P3 — ✅ Voice note transcription adapter  · _task #9 — DONE_
MVP item "text + **voice notes**". No inbound audio path before this.
- [x] Inbound audio (WhatsApp voice) → text via faster-whisper (local, CPU/int8)
- [x] Normalize into existing text intake path (transcribe once at `/route`
      entry → mutate `payload.message.text` so routing, vitals, classification,
      clinical triage, and message_log all see the transcript)
- **SoT ref:** MVP #1, §3A (voice glucose), §3C (voice trigger diary)

### P4 — ✅ Pregnancy timeline engine  · _task #10 — DONE_
Was triage-aware only (`pregnancy_checklist` intent + red-flag routing). Now a
full timeline engine.
- [x] LMP/due-date field + gestational-week computation (`pregnancies` table +
      pure math module: EDD/Naegele, gestational age, trimester)
- [x] Milestone materializer sweep (visits/labs/scans/supplements + weekly
      check-ins) — rides the dose-materialize loop, idempotent like labs
- [x] Wire `pregnancy_weekly_v1` (created the template + dispatcher branches)
- [x] Intake/management endpoints (`POST/GET/end` pregnancy) with eager
      materialize
- _Future: conversational NL intake ("pregnant, LMP 15 Jan") + data-aware
  `pregnancy_checklist` reply (current week + next milestone). Logged under #13._
- **SoT ref:** §3F, V1 deliverable "Pregnancy timeline pack", Epic 6

### P5 — ✅ Asthma cohort pack + first-class cohort flags  · _task #11 — DONE_
Asthma was entirely absent; cohorts were only `diabetes/cardiac/fall_risk`.
- [x] Migration: add cohort flags (asthma, post_op, pregnancy) + backfill
      `cohort_pregnancy` from active pregnancies; wired into broadcast +
      care-plan allowlists + onboarding + the pregnancy endpoints (flag sync)
- [x] Asthma rescue-usage tracking (regex parse → `metric_observation` +
      rolling-7d poor-control detection → idempotent `asthma_control` ops
      ticket) + trigger diary (`asthma_trigger_logs`) — both routed like
      vitals (graph node + sync fallback, safety-deferred), voice-compatible
- _Controller reminders already covered by the existing dose-reminder engine
  (a controller inhaler is a regimen). Puff-based refill estimation deferred
  (overlaps the refill/partner layer); logged under #13._
- **SoT ref:** §3C, V1 "Asthma rescue/puff analytics", Epic 6

### P6 — ✅ Partner / refill execute layer  · _task #12 — DONE_
The `Order`/`OrderStatus` schema was dead (defined in 0001, never written).
Now activated end-to-end.
- [x] Refill "reorder" action → create `Order` (regex `reorder` + handler
      branch + opt-in "Reorder" button via `REFILL_REORDER_ENABLED`)
- [x] Pharmacy partner adapter — replaceable `PharmacyAdapter` Protocol +
      default `DeepLinkPharmacyAdapter` (env-configurable deep-link) +
      `get_pharmacy_adapter` factory. No in-chat payments (§11) — we route to
      the partner.
- [x] Substitution approval flow — `propose-substitution` endpoint → enqueue
      → dispatcher renders `substitution_approval_v1` (template created) →
      patient Approve/Decline (`[order-action]` tap → `order_handler`, graph +
      sync routes)
- [x] Order management endpoints: create (adapter + dedupe), list, status
      advance
- _Migration 0040 drops the dead empty `orders` table + its `order_status` PG
  enum and recreates it with the execute-layer schema (String status, regimen
  link, med/dose snapshots, substitution columns)._
- **SoT ref:** MVP #6, Epic 4, §2 Monetization. (No in-chat payments — §11.)

---

## Half-finished within shipped features  · _task #13_

| Item | State | Evidence |
|---|---|---|
| ✅ Goal auto-achievement (N consecutive on-target) | DONE — drift sweep flips active→achieved + resolves drift ticket | `goal_drift_sweep.py` |
| ✅ Clinical-alert on-call rota + multi-doctor escalation | DONE — re-pages escalate round-robin through the on-call rota, excluding the last-paged doctor | `clinical_alert_pager.py` |
| ✅ Calendar sync via webhook push | DONE — `POST /webhooks/google-calendar` push-triggers a per-doctor sync (polling remains backstop) | `main.py` + `calendar_sync_sweep.reconcile_doctor` |
| ✅ Broadcast cohort-tag / composite filters | DONE — all_of/any_of composite + cohort_tag_id, 6 cohorts | `broadcast_service.py` |
| ✅ Multi-patient households | DONE — `households` table + `patients.household_id` + create/add-member/lookup endpoints (1 caregiver → many patients) | `households.py` |
| ✅ Refill/delivery coordination (senior) | DONE via P6 — reorder→Order, pharmacy adapter, status lifecycle (pending→…→delivered), delivery receipts. Senior cohort uses the same execute layer. | P6 / `orders.py` |
| ✅ Post-op completion checklist + wound-photo→queue | DONE — `post_op_episodes` + day-N checklist materializer + `post_op_check_v1` + wound-photo→`wound_review` ticket | SoT §3D |
| ✅ Patient-facing receipts | DONE — order delivery receipt (`order_receipt_v1`) on status→delivered | MVP #7 |
| ✅ Proactive weekly-trend push (from P2) | DONE — opt-in sweep + `weekly_trend_v1`, deduped per 7d | `weekly_trend_sweep.py` |

---

## Test / infra debt  · _task #14 — DONE_
- ✅ **Parallelism via `pytest-xdist`.** Added `pytest-xdist` to the `dev`
  extra + registered a `serial` marker. A `conftest` collection hook
  auto-tags the global-state sweep modules (goal-drift, adherence-pattern,
  care-gaps, recap, service-health, weekly-trend, clinical-alert-pager) — the
  ones that scan whole tables and would cross-contaminate counts — plus any
  `@pytest.mark.serial` test, and assigns them a shared `xdist_group`. The
  rest of the suite (uniquely-suffixed, per-entity tests) fans out safely.
  - **Run parallel:** `pytest -n auto --dist loadgroup` (serial sweeps pinned
    to one worker; everything else parallel). Verified: 589 unit pass under
    `-n auto`; a mixed integration subset (parallel + serial) green under
    `-n 4 --dist loadgroup`.
- ✅ **Skipped scheduler tests** — both were un-skipped in P1 (refill contract
  + bridge-fixture rework). No debt skips remain (only 2 benign runtime
  `pytest.skip` precondition-guards in `test_analytics.py`).

---

## Deferred (correctly out of scope — SoT §11)
- ⏸️ ABDM/ABHA linkage
- ⏸️ In-chat payments
- ⏸️ Autonomous clinical diagnosis/treatment

---

## Progress log
- 2026-05-23 — **P6 partner / refill execute layer: DONE.** Activated the dead
  `Order` schema. Migration 0040 drops the empty 0001 `orders` table + its
  `order_status` PG enum and recreates it with the execute-layer shape (String
  status, `regimen_id` link, med/dose snapshots, partner deep-link, +
  substitution columns). New `app/db/repositories/orders.py` (create/list/
  get_open_for_regimen dedupe/set_status/propose+resolve_substitution, with
  `await refresh` after mutating flushes to avoid the onupdate lazy-load
  MissingGreenlet). New `services/orchestrator/pharmacy.py` — a replaceable
  `PharmacyAdapter` Protocol + default `DeepLinkPharmacyAdapter`
  (env-configurable deep-link, URL-encoded) + `get_pharmacy_adapter` factory
  (no in-chat payments per §11; we route to the partner). Refill "reorder":
  `_REFILL_ACTION_RE` now accepts `reorder`, a handler branch calls the adapter
  + creates an Order (idempotent via get_open_for_regimen), and an opt-in
  "Reorder" button (`REFILL_REORDER_ENABLED`) replaces Snooze on the reminder
  (default off → legacy 3-button set + its test unchanged). Substitution loop:
  `POST /orders/{id}/propose-substitution` records the proposal + enqueues an
  `order_substitution_request`; the dispatcher renders it as the new
  `substitution_approval_v1` template (out-of-CSW) / Approve-Decline buttons
  (in-CSW); the patient's tap (`[order-action] sub_approve|sub_decline`) routes
  to a new `order_handler` (graph node + sync-fallback short-circuit) which
  resolves the substitution. Order endpoints: create (adapter + 409 dedupe),
  list, status advance. Tests: 6 unit (adapter env variations, marker parsers)
  + 10 integration (order CRUD, reorder→Order idempotent, substitution
  propose→enqueue→approve/decline, dispatcher template/freeform, /route e2e).
  583 unit pass; orders + scheduler + inbox integration green; ruff clean (new
  files). Migration applied to remote DB (0039→0040). **Next: P7+ — half-
  finished completions (#13) + test infra (#14).**
- 2026-05-23 — **P5 asthma cohort + first-class cohort flags: DONE.** Migration
  0039 adds `cohort_asthma` / `cohort_post_op` / `cohort_pregnancy` booleans to
  patients (backfilling pregnancy from active episodes) + an
  `asthma_trigger_logs` table. The three flags are wired into the broadcast
  cohort allowlist, care-plan `KNOWN_COHORT_ATTRS`, `update_onboarding`, and the
  pregnancy create/end endpoints (which now keep `cohort_pregnancy` in sync).
  New `services/orchestrator/asthma_handler.py` — conservative regex parsers for
  rescue-inhaler use ("used my reliever 3 times", "2 puffs salbutamol"; a bare
  "took my inhaler" is treated as a *controller* dose, not rescue) and the
  trigger diary ("trigger: dust"). Rescue use → `metric_observation`
  (rescue_inhaler_puffs) + a rolling-7-day control check (≥8 puffs OR ≥3 days)
  that opens an idempotent `asthma_control` ops ticket; triggers →
  `asthma_trigger_logs`. Routed exactly like vitals — new `asthma_handler` graph
  node + sync-fallback short-circuit, ranked AFTER side-effect so a rescue
  report bundled with a symptom routes to triage first. Works over transcribed
  voice (rides P3). Tests: 8 unit (parsers: counts, word-counts, controller
  rejection, trigger variants) + 8 integration (persist, poor-control
  idempotent ticket, trigger diary, /route e2e, safety deferral, asthma-cohort
  broadcast targeting, pregnancy-flag sync). 577 unit pass; asthma + routing
  regression green; ruff clean (new files). Migration applied (0038→0039).
  **Next: P6 partner / refill execute layer.**
- 2026-05-23 — **P4 pregnancy timeline engine: DONE.** New `pregnancies` table
  (migration 0038, partial unique index = one active pregnancy per patient) +
  `Pregnancy`/`PregnancyStatus` model + repo. Pure math module
  `services/orchestrator/pregnancy.py` (EDD via Naegele's 280-day rule,
  gestational age, trimester, a 17-entry ANC milestone schedule, weekly
  check-in + focus copy) — fully unit-tested. Milestone materializer
  `services/scheduler/pregnancy_milestones.py` walks active pregnancies and
  enqueues `pregnancy_milestone_due` + `pregnancy_weekly_due` events
  (future-only, idempotent dedupe by milestone-key/GA-week, rolling 2-week
  weekly horizon); rides the existing dose-materialize loop. Dispatcher branches
  + freshness windows + the new `pregnancy_weekly_v1` Meta template (3 params:
  name/week/focus) — milestone + weekly sends share it, freeform in-CSW /
  template out. Intake endpoints: `POST /patients/{id}/pregnancy` (resolve
  LMP↔EDD, create, eager-materialize, return summary w/ current week +
  trimester + next milestone), `GET …/pregnancy`, `POST …/pregnancy/{id}/end`
  (cancels pending reminders). Tests: 14 unit (math + materializer scheduling/
  dedup, whisper-free), 9 integration (endpoints + eager materialize + end-
  cancel + dispatcher builders template/freeform/ReminderNotApplicable). 569
  unit pass; pregnancy + scheduler integration green; ruff clean (new files),
  no new lint on touched files. Migration applied to remote DB (0037→0038).
  **Next: P5 asthma cohort + first-class cohort flags.**
- 2026-05-23 — **P3 voice note transcription: DONE.** New
  `services/orchestrator/transcription.py` — parses the ingress `[voice-note]
  public_path=… mime=…` marker (mirrors the prescription-image path),
  resolves the audio (shared upload volume or download via
  `PUBLIC_MEDIA_BASE_URL`), and transcribes locally with import-guarded
  faster-whisper (CPU/int8, model via `WHISPER_MODEL`, default `base`). Added
  as an optional `[voice]` extra; when absent (or on any decode failure) it
  degrades to a "please type" fallback — never drops the message. Transcription
  happens ONCE at the `/route` entry (off-loop via `asyncio.to_thread`),
  mutating `payload.message.text` so the workflow, vitals short-circuit, inbox
  classification, clinical-alert triage, AND message_log all operate on the
  transcript — a spoken "severe chest pain" reaches triage (the triage gate now
  also fires for `input_kind=voice`). Original marker snapshotted into
  `metadata.voice_marker`; inbound badged `input_kind=voice` for the ops inbox.
  Defensive re-transcription also wired into the graph's `_ingest_node` + the
  sync-fallback `run_agent_workflow` for direct callers. Tests: 7 unit (marker
  parse + maybe_transcribe orchestration with mocked whisper) + 2 integration
  (voice→vitals observation + voice badge; unavailable-transcription fallback).
  555 unit pass; voice/vitals/inbox/clinical-alert integration green; ruff clean
  (new files); no new lint on touched files. **Next: P4 pregnancy timeline.**
- 2026-05-10 — **P2 vitals self-report: DONE.** New
  `services/orchestrator/vitals_handler.py` — regex parser for glucose / BP
  (→2 readings) / weight / HbA1c / peak-flow with plausibility ranges;
  persists `metric_observation` (source=patient_self_report), links to a
  matching active `care_plan_goal`, replies with ack + on/off-target + a
  light weekly count. Wired into the agent graph (new `vitals_handler` node +
  router rank AFTER side-effect for safety) AND the sync-fallback runner
  (defers to triage when the message also reads as a symptom). Tests: 9 unit
  (parser) + 7 integration (persistence, goal-link, BP-pair, /route e2e,
  safety deferral). 548 unit pass, ruff clean. _Note: inline weekly count
  shipped; a proactive scheduled weekly-trend push is a future add (logged
  under task #13)._
- 2026-05-10 — Doc created from vision audit. Starting P1 (refill contract bug).
- 2026-05-10 — **P1 refill contract bug: code-complete.** `RefillDueRequest`
  now carries `regimen_id` + `dose`; `/emit-refill-due` builds a
  dispatcher-shaped payload (`stage` key, `regimen_id`, `dose`);
  `test_tick_dispatches_due_events_and_marks_dispatched` unskipped + reworked
  to seed a real patient+regimen. (Integration was briefly blocked by a
  Supabase auto-pause; restored.)
- 2026-05-10 — **P1 DONE + bridge fix (task #14 partial).** Verified end-to-end
  after DB restore. Three follow-on fixes uncovered during verification:
  (1) `WHATSAPP_DRY_RUN=1` hard-set in conftest so gateway `/send` never hits
  real Meta in tests (.env ships `=0` for dev sends); (2) reworked the scheduler
  test bridge from a sync TestClient to a real async `httpx.ASGITransport` —
  removes the BlockingPortal same-thread crash; (3) un-skipped BOTH previously
  skipped scheduler tests + restored `test_persistence` Meta send test. Result:
  `test_scheduler` 4/4, dispatch-path regression 27/27 green. **Next: P2 vitals
  self-report.**
