/**
 * HMAC-SHA256 signing for the X-Ops-Actor header.
 *
 * The orchestrator's shared-API-key model lets us assert any operator name in
 * X-Ops-Actor — fine for audit attribution as long as the ops console itself
 * is trusted, but a compromised orchestrator API key would let an attacker
 * forge any actor.
 *
 * When OPS_ACTOR_SIGNING_KEY is set on BOTH the orchestrator and the ops
 * console, we sign the actor with HMAC-SHA256 and send the hex digest in
 * X-Ops-Actor-Signature. The orchestrator's app/operator_signature.py uses
 * the same algorithm to verify. With OPS_ACTOR_SIGNATURE_REQUIRED=1 set on
 * the orchestrator, unsigned headers are rejected with 401.
 *
 * Server-only — the signing key must never reach the browser.
 */

import "server-only";
import { createHmac } from "node:crypto";

/**
 * Compute lowercase-hex HMAC-SHA256(key, actor).
 *
 * Mirrors {@link app.operator_signature.sign} in the Python codebase so a
 * round-trip from ops-console → orchestrator verifies cleanly.
 */
export function signActor(actor: string, key: string): string {
  if (!key) {
    throw new Error("signing key is required");
  }
  return createHmac("sha256", key).update(actor, "utf8").digest("hex");
}

/**
 * Build the (X-Ops-Actor, X-Ops-Actor-Signature) header pair for an outbound
 * call. Returns an empty object when no actor is supplied; returns just
 * X-Ops-Actor when the signing key isn't configured (the orchestrator will
 * accept the unsigned header unless OPS_ACTOR_SIGNATURE_REQUIRED=1).
 */
export function actorHeaders(actor: string | undefined): Record<string, string> {
  if (!actor) return {};
  const key = process.env.OPS_ACTOR_SIGNING_KEY ?? "";
  const headers: Record<string, string> = { "x-ops-actor": actor };
  if (key) {
    headers["x-ops-actor-signature"] = signActor(actor, key);
  }
  return headers;
}
