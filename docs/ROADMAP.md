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

### P1 — 🔴 Refill emit/dispatch contract bug  · _task #7_
A real defect, not just test debt. `/emit-refill-due` (`RefillDueRequest`)
doesn't accept `regimen_id`, but `services/scheduler/dispatcher.py` raises
`"refill payload missing regimen_id"` and skips the event — so **refill
reminders never dispatch**. Hidden behind a skipped test
(`test_scheduler.py::test_tick_dispatches_due_events_and_marks_dispatched`).
- [ ] Thread `regimen_id` through request → scheduled_event payload → dispatcher
- [ ] Unskip the test
- **SoT ref:** MVP #4 (refill forecasting), Epic 4

### P2 — 🟡 Patient vitals self-report inbound flow  · _task #8_
The SoT's daily-retention engine. Patients text `"sugar 140"` / `"BP 130/85"`
and get weekly trends. Storage exists (slice 14 `metric_observations` with a
`patient_self_report` source value) but **no inbound flow writes it** — the
enum value has nothing producing it.
- [ ] Inbound handler: parse reading from message → `metric_observation`
- [ ] Match to active `care_plan_goal` by metric
- [ ] Weekly trend summary surfaced to patient
- **SoT ref:** §3A/§3B (glucose/BP capture + trend), MVP #1

### P3 — 🔴 Voice note transcription adapter  · _task #9_
MVP item "text + **voice notes**". No inbound audio path today. `faster-whisper`
is already available transitively (graphify dep).
- [ ] Inbound audio (WhatsApp voice) → text via faster-whisper
- [ ] Normalize into existing text intake path
- **SoT ref:** MVP #1, §3A (voice glucose), §3C (voice trigger diary)

### P4 — 🟡 Pregnancy timeline engine  · _task #10_
Triage-aware today (`pregnancy_checklist` intent + red-flag routing) but **no
trimester timeline, no milestone scheduler**, and `pregnancy_weekly_v1`
template is defined yet never sent.
- [ ] LMP/due-date field + gestational-week computation
- [ ] Milestone materializer sweep (visits/labs/scans/supplements)
- [ ] Wire `pregnancy_weekly_v1`
- **SoT ref:** §3F, V1 deliverable "Pregnancy timeline pack", Epic 6

### P5 — 🔴 Asthma cohort pack + first-class cohort flags  · _task #11_
Asthma entirely absent. Cohort flags are only `diabetes/cardiac/fall_risk`;
**asthma/post_op/pregnancy have no first-class flag.**
- [ ] Migration: add cohort flags (asthma, post_op, pregnancy)
- [ ] Asthma flows: controller reminders, rescue-usage tracking, trigger
      diary, puff-based refill estimation
- **SoT ref:** §3C, V1 "Asthma rescue/puff analytics", Epic 6

### P6 — 🔴 Partner / refill execute layer  · _task #12_
Biggest scope + monetization wedge. The `Order`/`OrderStatus` schema
(`models.py:324`) is **dead — defined, never written**. `substitution_approval_v1`
template exists with no flow.
- [ ] Refill "reorder" action → create `Order`
- [ ] Pharmacy partner adapter (deep-link/API, replaceable interface)
- [ ] Substitution approval flow
- **SoT ref:** MVP #6, Epic 4, §2 Monetization. (No in-chat payments — §11.)

---

## Half-finished within shipped features  · _task #13_

| Item | State | Evidence |
|---|---|---|
| 🟡 Goal auto-achievement (N consecutive on-target) | manual status only | `models.py:975` |
| 🟡 Clinical-alert on-call rota + multi-doctor escalation | re-pages same doctor; `is_on_call` boolean only | slice 11 |
| 🟡 Calendar sync via webhook push | polling only | `calendar_sync_sweep.py:34` |
| 🟡 Broadcast cohort-tag / composite filters | legacy 3 cohorts only | `broadcast_service.py:58` |
| 🔴 Multi-patient households | caregivers exist; no 1→many model | SoT V1 |
| 🟡 Refill/delivery coordination (senior) | reminders only, no logistics | SoT §3E |
| 🔴 Post-op completion checklist + wound-photo→queue | side-effect triage only | SoT §3D |
| 🟡 Patient-facing receipts | audit trail exists; receipts unclear | MVP #7 |

---

## Test / infra debt  · _task #14_
- 🔴 Full 48-file integration suite >1.5h over Supabase; hangs on global-state
  tests. Needs `pytest-xdist` per-worker isolation or local-Postgres path.
- 🟡 2 skipped scheduler tests (one = the P1 refill bug; one = bridge-fixture
  threading rework).

---

## Deferred (correctly out of scope — SoT §11)
- ⏸️ ABDM/ABHA linkage
- ⏸️ In-chat payments
- ⏸️ Autonomous clinical diagnosis/treatment

---

## Progress log
- 2026-05-10 — Doc created from vision audit. Starting P1 (refill contract bug).
- 2026-05-10 — **P1 refill contract bug: code-complete.** `RefillDueRequest`
  now carries `regimen_id` + `dose`; `/emit-refill-due` builds a
  dispatcher-shaped payload (`stage` key, `regimen_id`, `dose`);
  `test_tick_dispatches_due_events_and_marks_dispatched` unskipped + reworked
  to seed a real patient+regimen. Verified: ruff clean, 537/539 unit tests
  pass. ⚠️ Integration verification BLOCKED — Supabase project is **paused**
  (pooler reachable but tenant not found); the 2 remaining unit failures are
  `test_session.py` DB-connectivity tests, same cause. **Action needed: restore
  the Supabase project** (dashboard) to run the integration suite.
