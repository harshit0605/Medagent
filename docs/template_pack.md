# WhatsApp Utility Templates (MVP)

## Existing baseline templates
- care_opt_in_v1
- prescription_request_v1
- dose_reminder_v1
- dose_missed_followup_v1
- refill_due_v1
- substitution_approval_v1
- lab_due_v1
- appointment_followup_v1
- pregnancy_weekly_v1
- caregiver_missed_streak_v1
- escalate_call_v1

## Sprint 1 additions (Smart Miss Recovery + Refill Reliability)
- dose_miss_reason_prompt_v1
- refill_due_d7_v1
- refill_due_d3_v1
- refill_due_d1_v1

## Sprint 2 additions (Cohort Triage + Caregiver Intelligence)
- triage_alert_v1
- caregiver_daily_digest_v1

## Sprint 3 additions (Closure Loops + Program Analytics)
- lab_closure_update_v1
- appointment_closure_update_v1

## Caregiver alerts (V5 — SoT §3B)
- `caregiver_missed_streak_v1` (Utility) — 2 body params: caregiver name, and a
  "{patient} has missed several doses of {med}" phrase. Sent to a confirmed
  caregiver when a cardiac (HTN) patient hits a missed-dose escalation streak.
  Gated by `HTN_CAREGIVER_ALERT_ENABLED` (default on; naturally scoped to
  cardiac-cohort patients with confirmed caregivers).

## Postpartum (V3 extension)
- `postpartum_check_v1` (Utility) — 3 body params: name, postpartum_week, focus. Used by both the postpartum weekly check-in (`postpartum_weekly_due`) and the postpartum milestone reminders (`postpartum_milestone_due` — early visit, EPDS mental-health screen, 6-week visit + contraception counsel, 8-week baby vaccines, 12-week close). Param shape mirrors `pregnancy_weekly_v1` so a single Meta-approved template can serve both phases if needed.

### Template drafting notes
- `dose_miss_reason_prompt_v1` should ask for one structured reason only:
  `FORGOT`, `SIDE_EFFECT`, `OUT_OF_STOCK`, `CONFUSED`, `COST`, `OTHER`.
- Refill ladder templates should keep same action CTA: `REORDER` or `UPDATE COUNT`.
- For symptom-risk contexts, include `CALL` escalation in body.
- `caregiver_daily_digest_v1` should summarize misses in last 24h and open high-risk alerts.

Use utility category for reminders and follow-ups; enforce template sends outside 24h customer service window.

## Multi-language (V5 — I3)
The dispatcher sets each template send's `language` code from the patient's
`preferred_language`; Meta serves the matching language version of the named
template. To enable a language for a template, submit + get approval for that
language version of the SAME template name at Meta (e.g. `dose_reminder_v1` in
`hi`, `ta`, `te`, `kn`, `mr`). Until a language version is approved, Meta falls
back per its own rules; set `WHATSAPP_TEMPLATE_LANGUAGE` for the default.
Submit-once-per-language is an ops action — the code path is ready.
