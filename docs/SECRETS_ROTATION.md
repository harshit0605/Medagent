# Secrets rotation runbook

How to rotate each secret Medagent depends on, with zero or minimal downtime.
All secrets live in the Coolify per-application env (and mirrored in local
`.env` for dev). None are committed — `.env` is gitignored and gitleaks runs
in CI.

> **Golden rule:** rotate one secret at a time, verify, then move on. Never
> rotate two interdependent secrets in the same deploy.

---

## 1. `MEDAGENT_FERNET_KEY` — at-rest OAuth-token encryption

Encrypts doctors' Google Calendar refresh tokens at rest. Supports
**multi-key rotation** via `MultiFernet` (`app/security/crypto.py`), so you can
rotate without a flag-day re-encrypt.

1. **Generate** a new key:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. **Demote + promote**, on the **orchestrator** app (the only service that
   decrypts tokens):
   - Move the current value of `MEDAGENT_FERNET_KEY` into
     `MEDAGENT_FERNET_KEYS_OLD` (comma-separated if there are several).
   - Set `MEDAGENT_FERNET_KEY` to the new key.
   - Redeploy. New writes use the new key; existing ciphertexts still decrypt
     via the demoted key.
3. **Backfill** — re-encrypt stored ciphertexts to the new primary using
   `app.security.crypto.rotate_token(ciphertext)` over the doctor-OAuth rows
   (a one-off script; it never exposes the plaintext).
4. **Drop the old key** — once the backfill is done and verified, remove
   `MEDAGENT_FERNET_KEYS_OLD` and redeploy.

_Verify:_ a doctor whose token was written under the old key can still trigger
a calendar sync at each step. `tests/test_crypto.py` covers the key-overlap +
post-rotation semantics.

---

## 2. `ORCHESTRATOR_API_KEY` / `GATEWAY_API_KEY` — internal service auth

Shared secrets the ops console + scheduler present to the orchestrator/gateway.
No multi-key support, so rotate with a brief dual-accept window using a deploy
ordering that never leaves a caller unable to authenticate:

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Set the new key on the **validating** service first is NOT safe (it would
   reject in-flight callers). Instead: the services fail closed but accept a
   single key, so do a **rolling** swap during a low-traffic window:
   - Set the new value on **both** the validating service AND every caller
     (orchestrator app + ops console + scheduler) in one Coolify batch, then
     redeploy them together. Coolify restarts are fast (seconds); a handful of
     calls may 401 during the overlap and retry.
3. _Verify:_ `GET https://api.ramkaaj.com/health/ready` (200), then an ops
   console action that round-trips to the orchestrator (e.g. load the patients
   list).

> Future hardening: give these the same MultiFernet-style dual-key accept the
> Fernet key has, so rotation is zero-downtime. Tracked in ROADMAP.

---

## 3. `OPS_ACTOR_SIGNING_KEY` — HMAC-signed operator identity

Signs the `X-Ops-Actor` header. Shared between the orchestrator (verifies) and
the ops console (signs). Rotating it is a 2-step dance because
`OPS_ACTOR_SIGNATURE_REQUIRED=1` is enforced in prod:

1. Temporarily set `OPS_ACTOR_SIGNATURE_REQUIRED=0` on the orchestrator (it now
   accepts unsigned + any-signed; `details.signed` records the truth).
2. Set the new `OPS_ACTOR_SIGNING_KEY` on **both** orchestrator + ops console;
   redeploy both.
3. Re-enable `OPS_ACTOR_SIGNATURE_REQUIRED=1` on the orchestrator.

_Verify:_ a privileged ops-console action (e.g. DSAR export) succeeds and its
`operator_actions.details.signed == true`.

---

## 4. `OPENAI_API_KEY` — LLM

No coordination needed (single consumer, the orchestrator). Set the new value,
redeploy the orchestrator. The LLM **circuit breaker** means a brief blip falls
back to the deterministic path rather than erroring patients.

_Verify:_ `GET /health/ready` reports `checks.llm.ok == true`.

---

## 5. `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_APP_SECRET` — Meta

- **Access token**: rotated in the Meta dashboard. Update the gateway (sends)
  + ops console (webhook). The Meta send retry/backoff absorbs the swap window.
- **App secret**: used to verify inbound webhook signatures
  (`X-Hub-Signature-256`). Update the ops console; Meta signs new requests with
  the new secret immediately, so deploy promptly to avoid rejecting inbound.

---

## Revoking a leaked secret

If a secret is suspected compromised, rotate it **immediately** (don't wait for
a window) and:
- For `OPS_ACTOR_SIGNING_KEY` / API keys: the leak window's `operator_actions`
  rows are the audit trail of what the holder could have done.
- For `DSAR_EXPORT_DAILY_LIMIT`: an abnormal-export-volume `dsar_export_abuse`
  ops ticket is the early-warning signal that a key may already be in use.
