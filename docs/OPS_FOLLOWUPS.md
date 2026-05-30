# Operational follow-ups

Non-code actions whose code paths are READY but which need an ops/admin step,
an external integration, or a business decision to activate. Each names the
exact lever.

---

## J1 — Submit `caregiver_dose_reminder_v1` + caregiver/streak templates to Meta

The caregiver dose fan-out (V4) and the HTN caregiver streak alert (V5) are
fully built and gated OFF until their templates are approved at Meta.

1. Submit + get approval for these Utility templates (param shapes in
   `docs/template_pack.md`):
   - `caregiver_dose_reminder_v1` (2 params: patient name, medication)
   - `caregiver_missed_streak_v1` (2 params: caregiver name, streak phrase)
2. Flip `CAREGIVER_DOSE_FANOUT_ENABLED=1` on the scheduler once the dose
   template is live (the streak alert defaults on but no-ops without consented
   caregivers + the template).
3. Verify: a consented + opted-in caregiver receives a dose copy; a cardiac
   patient's missed-streak escalation reaches their caregiver.

## J2 — Coolify deploy gate on CI status

Branch protection on `main` already requires the `ci` checks to pass before a
PR merges, so merged code is green. But Coolify still auto-deploys on every
push to `main` (including a direct push that bypasses a PR). Decision:

- **Accept it** (current): merges are gated, direct pushes are the only gap,
  and the team pushes via PRs. Lowest effort.
- **Tighten**: point Coolify at a `deploy` branch that only fast-forwards from
  `main` after CI is green, or use a GitHub Action that triggers the Coolify
  deploy webhook only on a successful `ci` run. ~half a day.

## Multi-language template versions (I3 follow-on)

The dispatcher selects a template's language from the patient's
`preferred_language` (V5). To actually serve a non-English version, submit +
get approval for that language version of the SAME template name at Meta
(e.g. `dose_reminder_v1` in `hi`, `ta`, `te`, `kn`, `mr`). Submit-once-per-
language per template. Until approved, Meta falls back per its own rules.

## HMAC + dual-control + budget toggles (recap)

These are LIVE-able via env, documented in `.env.example` +
`docs/SECRETS_ROTATION.md`:

- `OPS_ACTOR_SIGNATURE_REQUIRED=1` — enforced in prod (done).
- `ERASURE_DUAL_CONTROL=1` — opt-in two-person erasure.
- `DSAR_EXPORT_DAILY_LIMIT` — per-operator DSAR ceiling (default 20).
- `LLM_PATIENT_DAILY_TOKEN_BUDGET` — per-patient LLM spend cap (default off).
- `ERASURE`/Fernet rotation — `MEDAGENT_FERNET_KEYS_OLD` for zero-downtime roll.

---

## Scoped product follow-ups (code not yet written)

Captured during the V5 sprint as genuinely larger initiatives — each needs an
external integration or a substantial refactor, so they were scoped rather than
half-built:

| Item | Why deferred | First step |
|---|---|---|
| **G2** symptom check-in flows | New periodic-prompt scheduler + tag taxonomy | A `symptom_checkin` sweep + cohort-keyed prompt set |
| **G3** patient-initiated appointment | Reuses booking, but needs slot-proposal UX | NL "book me Tue" → `availability_for_doctor` → propose slots |
| **G4** vaccination reminders | New schedule (pediatric/senior); pregnancy/PP vaccines already partly covered | A vaccine-schedule materializer like pregnancy_milestones |
| **G6** monthly spend snapshot | Orders don't capture cost (no in-chat payments per §11) | Add `cost` to Order + a pricing source first |
| **F1** mobile / PWA ops console | Front-end build (service worker + responsive shell) | `manifest.json` + offline shell for inbox + alerts |
| **F2** inbox voice / photo render | Front-end media rendering | Render `voice_marker` audio + wound-photo image inline |
| **F4** bulk patient actions | Front-end + a batch endpoint | `POST /patients/bulk-action` over a cohort filter |
| **F5** inbox SLA timer + snooze | Front-end (data exists: `sla_due_at`) | Surface the existing SLA fields + a snooze control |
| **I1** multi-tenant / multi-clinic | Large refactor (`clinic_id` on every entity + scoping) | Add nullable `clinic_id` + a tenancy filter dependency |
| **I2** SMS fallback for critical escalation | Needs an SMS provider (Twilio etc.) | A pluggable `SmsAdapter` Protocol like `PharmacyAdapter` |
| **I4** adherence-summary PDF | PDF generation + a template | A `/patients/{id}/adherence.pdf` endpoint (reportlab) |
| **I5** lab partner ingestion | Needs a lab-partner API + report parser | A `LabAdapter` Protocol + report → `LabFollowup` attach |
