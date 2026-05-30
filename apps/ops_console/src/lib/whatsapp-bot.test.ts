/**
 * Tests for the WhatsApp button-id → orchestrator-text decoder.
 *
 * This decode is the bridge between a patient's button tap and the
 * orchestrator's deterministic handlers. Correctness matters:
 *  - dose-action ids must produce the exact "[dose-action] ..." marker the
 *    orchestrator's deterministic handler short-circuits on (no LLM).
 *  - a malformed / empty-payload id must return null (drop), never a
 *    half-formed instruction that could mis-route.
 */

import { describe, expect, test } from "vitest";

import { missingWhatsAppEnv, textFromButtonId } from "./whatsapp-bot";

describe("textFromButtonId", () => {
  test("dose actions produce the deterministic marker", () => {
    expect(textFromButtonId("dose_taken:42")).toBe(
      "[dose-action] taken adherence_event_id=42",
    );
    expect(textFromButtonId("dose_snoozed:7")).toBe(
      "[dose-action] snoozed adherence_event_id=7",
    );
    expect(textFromButtonId("dose_skipped:1")).toBe(
      "[dose-action] skipped adherence_event_id=1",
    );
    expect(textFromButtonId("dose_late_taken:99")).toBe(
      "[dose-action] late_taken adherence_event_id=99",
    );
  });

  test("refill action produces its marker", () => {
    expect(textFromButtonId("refill_done:5")).toBe(
      "[refill-action] done regimen_id=5",
    );
  });

  test("appointment actions produce natural-language instructions", () => {
    expect(textFromButtonId("cancel_appt:3")).toBe(
      "Please cancel my appointment id 3.",
    );
    expect(textFromButtonId("reschedule_appt:3")).toBe(
      "Please reschedule my appointment id 3.",
    );
    expect(textFromButtonId("book_new")).toBe(
      "I want to book a new appointment.",
    );
  });

  test("slot booking decodes the pipe-delimited payload", () => {
    expect(
      textFromButtonId("slot_book:12|2026-06-01T09:00|2026-06-01T09:30"),
    ).toBe(
      "Please book the slot 2026-06-01T09:00 to 2026-06-01T09:30 with doctor 12.",
    );
  });

  test("empty payload returns null (dropped, not half-formed)", () => {
    expect(textFromButtonId("dose_taken:")).toBeNull();
    expect(textFromButtonId("cancel_appt:")).toBeNull();
    expect(textFromButtonId("refill_done:")).toBeNull();
  });

  test("malformed slot payload (wrong arity) returns null", () => {
    expect(textFromButtonId("slot_book:12|onlytwo")).toBeNull();
    expect(textFromButtonId("slot_resched:1|2|3")).toBeNull(); // needs 4 parts
  });

  test("unknown button id returns null", () => {
    expect(textFromButtonId("totally_unknown:1")).toBeNull();
    expect(textFromButtonId("")).toBeNull();
  });
});

describe("missingWhatsAppEnv", () => {
  test("reports missing required vars when unset", () => {
    const saved: Record<string, string | undefined> = {};
    const required = [
      "WHATSAPP_ACCESS_TOKEN",
      "WHATSAPP_APP_SECRET",
      "WHATSAPP_PHONE_NUMBER_ID",
      "WHATSAPP_VERIFY_TOKEN",
    ];
    for (const k of required) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
    try {
      const missing = missingWhatsAppEnv();
      // At least the four we cleared are reported.
      for (const k of required) {
        expect(missing).toContain(k);
      }
    } finally {
      for (const k of required) {
        if (saved[k] !== undefined) process.env[k] = saved[k];
      }
    }
  });
});
