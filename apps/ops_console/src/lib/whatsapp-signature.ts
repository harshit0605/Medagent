/**
 * Defense-in-depth `X-Hub-Signature-256` verification.
 *
 * The `@chat-adapter/whatsapp` library DOES verify signatures
 * before invoking our message handlers. But our webhook route
 * kicks off ``forwardStatuses(rawBody)`` IN PARALLEL with the
 * adapter call (statuses aren't a chat-sdk event, so we parse
 * them ourselves). Without this gate, a forged POST would have
 * its statuses parsed + forwarded to our gateway BEFORE the
 * adapter rejects the request — meaning an attacker could send
 * fake "delivered" / "failed" events for any wamid we've issued
 * and corrupt our delivery tracking metrics.
 *
 * This module exposes ``verifyWhatsAppSignature`` so the route
 * can pre-check before doing anything with the body. If the
 * adapter's check fails downstream, that's still belt-and-
 * suspenders — but the key invariant is that nothing
 * status-related runs against unsigned bytes.
 *
 * Constant-time comparison via Node's ``timingSafeEqual`` so a
 * timing attack on the secret can't leak bytes.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * @returns ``true`` only when the body's HMAC matches the header.
 *          ``false`` for: missing secret (callable from contexts
 *          where verification is intentionally bypassed),
 *          missing/malformed header, or HMAC mismatch.
 */
export function verifyWhatsAppSignature(
  rawBody: string,
  headerValue: string | null,
  appSecret: string | undefined,
): boolean {
  if (!appSecret) return false;
  if (!headerValue) return false;
  if (!headerValue.startsWith("sha256=")) return false;
  const provided = headerValue.slice("sha256=".length).trim();
  if (provided.length === 0) return false;

  const expectedHex = createHmac("sha256", appSecret)
    .update(rawBody)
    .digest("hex");

  // Convert both to fixed-length Buffers so timingSafeEqual can
  // run without leaking the length difference. Hex strings of
  // unequal length can't match anyway — return false up front.
  if (provided.length !== expectedHex.length) return false;

  try {
    const a = Buffer.from(provided, "hex");
    const b = Buffer.from(expectedHex, "hex");
    if (a.length !== b.length) return false;
    return timingSafeEqual(a, b);
  } catch {
    // Hex decode failure — treat as mismatch.
    return false;
  }
}
