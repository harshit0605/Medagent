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

### Template drafting notes
- `dose_miss_reason_prompt_v1` should ask for one structured reason only:
  `FORGOT`, `SIDE_EFFECT`, `OUT_OF_STOCK`, `CONFUSED`, `COST`, `OTHER`.
- Refill ladder templates should keep same action CTA: `REORDER` or `UPDATE COUNT`.
- For symptom-risk contexts, include `CALL` escalation in body.
- `caregiver_daily_digest_v1` should summarize misses in last 24h and open high-risk alerts.

Use utility category for reminders and follow-ups; enforce template sends outside 24h customer service window.
