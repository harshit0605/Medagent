/**
 * Tests for the X-Hub-Signature-256 verifier. Run via ``bun test``
 * — the ops_console doesn't have Jest configured, but bun has a
 * built-in Jest-compatible runner so a single test file works
 * without dependencies.
 *
 * The signature verifier is the gate for an ENTIRE class of
 * spoof attacks on our webhook (forged delivery statuses + forged
 * inbound messages). Every defensive branch matters: missing
 * secret, missing header, malformed prefix, length mismatch, hex
 * decode failure, and most importantly — actual HMAC mismatch.
 */

import { describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";

import { verifyWhatsAppSignature } from "./whatsapp-signature";

const SECRET = "test-app-secret-do-not-deploy";

function sign(body: string, secret: string = SECRET): string {
  return (
    "sha256=" +
    createHmac("sha256", secret).update(body).digest("hex")
  );
}

describe("verifyWhatsAppSignature", () => {
  test("accepts a correctly-signed body", () => {
    const body = JSON.stringify({ entry: [{ changes: [] }] });
    expect(
      verifyWhatsAppSignature(body, sign(body), SECRET),
    ).toBe(true);
  });

  test("rejects a different body even with valid header", () => {
    const real = JSON.stringify({ entry: [] });
    const tampered = JSON.stringify({ entry: [{ malicious: true }] });
    // Header was computed for ``real`` but the body now claims to
    // be ``tampered`` — must reject.
    expect(
      verifyWhatsAppSignature(tampered, sign(real), SECRET),
    ).toBe(false);
  });

  test("rejects when secret is missing (defensive default-deny)", () => {
    const body = "anything";
    expect(verifyWhatsAppSignature(body, sign(body), undefined)).toBe(
      false,
    );
    expect(verifyWhatsAppSignature(body, sign(body), "")).toBe(false);
  });

  test("rejects when header is missing", () => {
    expect(verifyWhatsAppSignature("body", null, SECRET)).toBe(false);
  });

  test("rejects when header lacks the sha256= prefix", () => {
    const body = "body";
    // Same hex as sign(body) but without the ``sha256=`` prefix.
    const bareHex = createHmac("sha256", SECRET)
      .update(body)
      .digest("hex");
    expect(verifyWhatsAppSignature(body, bareHex, SECRET)).toBe(false);
  });

  test("rejects an empty signature value", () => {
    expect(verifyWhatsAppSignature("body", "sha256=", SECRET)).toBe(
      false,
    );
  });

  test("rejects when signed with a different secret", () => {
    const body = "body";
    expect(
      verifyWhatsAppSignature(body, sign(body, "wrong-secret"), SECRET),
    ).toBe(false);
  });

  test("rejects malformed hex (not decodable)", () => {
    // Right length but non-hex characters — hex decode throws,
    // verifier must catch and return false.
    const fake = "sha256=" + "Z".repeat(64);
    expect(verifyWhatsAppSignature("body", fake, SECRET)).toBe(false);
  });

  test("rejects mismatched length without crashing", () => {
    // 32 hex chars instead of 64 — should short-circuit before
    // timingSafeEqual would throw on Buffer length mismatch.
    const truncated = "sha256=" + "0".repeat(32);
    expect(
      verifyWhatsAppSignature("body", truncated, SECRET),
    ).toBe(false);
  });

  test("handles an empty body that's correctly signed", () => {
    const body = "";
    expect(
      verifyWhatsAppSignature(body, sign(body), SECRET),
    ).toBe(true);
  });

  test("handles unicode body bytes", () => {
    const body = JSON.stringify({ text: "नमस्ते दोस्त" });
    expect(
      verifyWhatsAppSignature(body, sign(body), SECRET),
    ).toBe(true);
  });
});
