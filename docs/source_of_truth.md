# Medagent Source of Truth (SoT)

_Last updated: 2026-02-15_

## 1) Product Vision

Build a **WhatsApp-first Care Concierge Agent** for:
- Medication adherence
- Refill orchestration
- Lab scheduling/follow-up
- Doctor follow-up reminders
- Pregnancy timeline support
- Senior-care + caregiver coordination

The agent handles logistics, reminders, structured intake, and routing — **not clinical diagnosis**.

## 2) Strategic Wedge

### Distribution
Onboard through clinic/hospital doctors (ENT, gynae, radiologist, chronic care providers).

### Retention
Daily utility from dose reminders, refill forecasts, and scheduled check-ins.

### Monetization
- **B2B**: clinic/hospital/pharmacy/diagnostics partnerships
- **B2C**: optional premium concierge programs

## 3) Target Cohorts and Workflows

### A) Diabetes
**Problems:** missed meds/insulin timing, refill lapses, irregular glucose logs, missed HbA1c cycles.

**Core flows:**
- Regimen reminders + Taken confirmations
- Glucose capture (manual + voice) and weekly trend summary
- HbA1c reminder cadence
- Days-left refill forecasting

**Escalate to human when:**
- Hypo/hyper symptoms
- Repeated missed doses
- Insulin confusion

### B) Hypertension (BP)
**Problems:** asymptomatic non-adherence, inconsistent BP logs.

**Core flows:**
- Habit nudges with light education
- BP logging 2–3x/week + trend snapshot
- Refill autopilot + caregiver alerts on missed streak

**Escalate when:**
- Very high readings
- Symptoms
- Missed-med streaks

### C) Asthma
**Problems:** controller vs rescue confusion, trigger tracking gaps.

**Core flows:**
- Controller schedule reminders
- Rescue usage tracking
- Trigger diary (text/voice)
- Puff-based refill estimation

**Escalate when:**
- Frequent rescue use
- Night awakenings
- Breathlessness/wheezing

### D) Post-op Antibiotics
**Problems:** early discontinuation, side effects, missed wound review.

**Core flows:**
- Strict timed reminders + completion checklist
- Side-effect triage prompts
- Optional wound photo upload routed to human queue

**Escalate when:**
- Fever/severe symptoms
- Missed >2 doses

### E) Senior Care
**Problems:** polypharmacy confusion, caregiver coordination gaps.

**Core flows:**
- Caregiver mode and daily digest
- Simple quick actions: Taken / Snooze / Call me
- Refill/delivery coordination

**Escalate when:**
- Repeated misses
- Confusion/adverse events

### F) Pregnancy (Pre/Post)
**Problems:** supplements + visit cadence, labs/scans timing, anxiety, postpartum coordination.

**Core flows:**
- Trimester timeline (visits/labs/scans/supplements)
- Red-flag symptom routing to human
- Postpartum meds, follow-ups, vaccine reminders

**Escalate when:**
- Any red-flag symptoms
- Mental health concerns
- Urgent issues

## 4) Scope and Milestones

### MVP (8–10 weeks)
1. WhatsApp chatbot (text + voice notes)
2. Regimen onboarding from manual entry and Rx upload
3. Reminder loop with Taken/Snooze/Skip
4. Refill forecasting (days-left)
5. Human escalation (doctor/pharmacist handoff)
6. Partner execute layer (API/deep-link checkout)
7. Audit trail and receipts

### V1 (12–16 weeks)
- Caregiver mode + multi-patient households
- Clinic program dashboards
- Asthma rescue/puff analytics
- Pregnancy timeline pack
- Optional ABDM/ABHA linkage

## 5) Mandatory Compliance and Safety Constraints

### WhatsApp Platform Constraints
- **24-hour window:** free-form replies only within 24h of last user message.
- **Outside 24h:** approved template required.
- **Automation:** must expose clear human escalation paths.
- **Cost model:** message-category pricing (design reminders as utility templates).
- **Regulated vertical caution:** avoid WhatsApp-native medicine commerce experiences.

### Medical Safety Constraints
- AI handles reminders/logistics/education/intake/routing only.
- Clinical decisions and urgent symptom resolution must be human-led.

## 6) Buildable Architecture (Python + LangGraph)

### Components
1. Channel gateway (WhatsApp Cloud API webhook)
2. LangGraph conversation orchestrator
3. Clinical ops console
4. Scheduler/event bus
5. Partner integrations (pharmacy/diagnostics/hospital)
6. Data layer (Postgres + object store + append-only audit)

### Inbound Conversation Graph Nodes
1. `ingest_message` (text/voice normalization)
2. `detect_intent`
3. `policy_gate`
4. `route` (adherence/regimen/refill/triage/handoff)
5. `compose_response`
6. `dispatch_message`

### Scheduler Graph Nodes
- `generate_daily_schedule`
- `send_reminder_template`
- `collect_confirmation`
- `missed_dose_detector`
- `refill_forecaster`

## 7) Canonical Data Model

Tables:
- `patients`
- `caregivers`
- `prescriptions`
- `regimens`
- `adherence_events`
- `alerts`
- `orders`
- `templates`

Each write path must preserve an auditable timestamped event trail.

## 8) WhatsApp Template Pack (Submission Baseline)

### Onboarding
- `care_opt_in_v1` (Utility)
- `prescription_request_v1` (Utility)

### Dose reminders
- `dose_reminder_v1` (Utility)
- `dose_missed_followup_v1` (Utility)

### Refill
- `refill_due_v1` (Utility)
- `substitution_approval_v1` (Utility)

### Labs and follow-up
- `lab_due_v1` (Utility)
- `appointment_followup_v1` (Utility)

### Pregnancy
- `pregnancy_weekly_v1` (Utility)

### Caregiver
- `caregiver_missed_streak_v1` (Utility)

### Human escalation
- `escalate_call_v1` (Utility)

Design rules:
- Keep messages short, transactional, non-promotional.
- Emphasize explicit action choices (e.g., Taken / Snooze / Skip / CALL).

## 9) 2026 WhatsApp AI Positioning Risk Mitigation

Position product as **clinic/pharmacy patient support workflow**, not generic AI assistant.

Mitigations:
- Scope statement in opt-in and template copy
- Utility-first operational messaging
- Always-present human escalation path

## 10) Delivery Plan (Codex-Ready Epics)

### Epic 0: Repo skeleton + schemas
- Gateway/orchestrator/scheduler services
- Message and event contracts
- End-to-end local flow

### Epic 1: WhatsApp integration
- Webhook verify, receive, send wrappers
- Template substitution
- Customer service window logic

### Epic 2: Regimen onboarding
- Guided manual flow
- Rx upload + parse stub
- Schedule generation

### Epic 3: Adherence loop
- Quick replies
- Event logging
- Missed-threshold ticketing + caregiver alert

### Epic 4: Refill + substitutions
- Days-left estimator
- Refill reminders
- Explicit substitution approval
- Partner deep links

### Epic 5: Ops console
- Queue and ticket detail
- Human actions (approve/message/call)

### Epic 6: Cohort packs
- Diabetes/BP/asthma/pregnancy logic bundles
- Weekly pregnancy timeline engine

## 11) Non-goals (Current Phase)

- No direct in-chat payments
- No autonomous clinical diagnosis/treatment
- No ABDM integration until explicitly prioritized

## 12) Engineering Rules of Engagement

- Enforce policy checks before every outbound send.
- Default to template mode when CSW state is unknown.
- Preserve all decision reasons in audit logs.
- Any red-flag or uncertain clinical signal should escalate to humans.
- Keep integrations replaceable via adapter interfaces.


## 13) Execution Roadmap (Sprint-by-Sprint)

### Sprint 1 — Smart Miss Recovery + Refill Reliability Foundation
- Missed-dose reason capture (`FORGOT`, `SIDE_EFFECT`, `OUT_OF_STOCK`, `CONFUSED`, `COST`, `OTHER`)
- Recovery routing (reschedule vs refill support vs human escalation)
- Refill stage ladder (D-7, D-3, D-1) with `REORDER / UPDATE COUNT` actions

### Sprint 2 — Cohort Triage Packs + Caregiver Intelligence
- Cohort-specific red-flag triage pathways (diabetes/BP/asthma/pregnancy/post-op)
- Caregiver permissions and daily digest summary
- SLA-aware escalation prioritization for operations

### Sprint 3 — Closure Loops + Program Analytics
- Lab/appointment closure tracking (due → booked → completed → reviewed)
- Clinic dashboards (adherence %, refill risk, follow-up closure)
- Programized care bundles for B2B/B2C packaging


## 14) Sprint 2 Implementation Notes (Delivered)

Delivered capabilities in this phase:
- Cohort-aware triage classification for diabetes, BP, asthma, pregnancy, and post-op flows.
- Severity-based escalation routing with queue priority + SLA targeting.
- Caregiver intelligence with daily digest (misses in last 24h + open high-risk alerts).

Operational intent:
- Keep symptom handling safety-first and route high/critical signals to human review quickly.
- Improve caregiver visibility while preserving constrained action permissions.


## 15) Sprint 3 Implementation Notes (Delivered)

Delivered capabilities in this phase:
- Follow-up closure loops for labs and appointments (`due -> booked -> completed -> reviewed`).
- Program dashboard aggregation with adherence rate, refill-risk rate, and follow-up closure rate.
- Closure-oriented reminder templates for lab and appointment progress updates.

Operational intent:
- Ensure reminders convert into completed care pathways, not just message opens.
- Provide clinics with measurable operational outcomes for program performance.


## 16) Sprint 4 Implementation Notes (Delivered)

Delivered capabilities in this phase:
- Persistence-ready schema additions for lab follow-ups, appointment follow-ups, and ops tickets.
- Ops ticket lifecycle primitives (open -> acknowledged -> resolved) with queue snapshot metrics.
- Alembic migration scaffold for follow-up and ops ticket tables to support durable operations workflows.

Operational intent:
- Move from in-memory-only workflow handling toward production persistence boundaries.
- Make operational queue handling measurable and auditable for clinic operations teams.


## 17) Sprint 5 Implementation Notes (Delivered)

Delivered capabilities in this phase:
- API-backed operations actions for ticket lifecycle management (`create`, `list`, `acknowledge`, `resolve`).
- Operations dashboard endpoint wiring for queue snapshot + program metrics in one payload.
- Thin service-level DTOs for ops responses so console and integrations can consume stable shapes.

Operational intent:
- Enable human teams to act on escalations through explicit API workflows rather than in-memory-only helper methods.
- Provide one integration-friendly dashboard endpoint for ops console views and periodic polling.


## 18) LangGraph/Agent Best-Practice Alignment (Current Implementation)

The orchestrator agent has been aligned to modern LangGraph-style workflow design:
- **Typed shared state** via a single `AgentState` contract to keep node I/O explicit.
- **Deterministic policy gate** before response composition (24h CSW window first, template fallback by default).
- **Safety-first routing** with explicit risk triage and escalation flags (`critical_red_flag`, `high_risk_symptom_report`).
- **Human-in-the-loop ready branch points** via `escalation_required` + `audit_reasons` surfaces.
- **Graph + fallback execution mode**: compile a `StateGraph` when LangGraph is available, otherwise run a deterministic fallback runner with equivalent semantics.

Implementation references:
- `services/orchestrator/agent_workflow.py`
- `services/orchestrator/main.py` (`/route` endpoint now delegates to the workflow runner)
