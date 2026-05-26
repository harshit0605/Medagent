/**
 * Tests for the X-Ops-Actor HMAC-SHA256 signing helper.
 *
 * The round-trip with the Python orchestrator (app/operator_signature.py)
 * has to be byte-identical or the verification gate rejects valid headers.
 * Two ways to break the round-trip:
 *   - hex case (Python uses lowercase; we must match)
 *   - UTF-8 encoding of the actor string
 * Both are exercised below.
 */

import { describe, expect, test } from "vitest";
import { createHmac } from "node:crypto";

import { actorHeaders, signActor } from "./operator-signature";

const KEY = "test-key-for-vitest";

function pyMatchingSig(actor: string, key: string): string {
  // Match exactly what the Python sign() produces:
  //   hmac.new(key.encode("utf-8"), actor.encode("utf-8"), sha256).hexdigest()
  // Node's createHmac().digest("hex") already produces lowercase hex; this
  // helper exists to make the cross-runtime contract explicit in the test.
  return createHmac("sha256", key).update(actor, "utf8").digest("hex");
}

describe("signActor", () => {
  test("returns a 64-char lowercase hex digest", () => {
    const sig = signActor("alice@clinic", KEY);
    expect(sig).toHaveLength(64);
    expect(sig).toMatch(/^[0-9a-f]{64}$/);
  });

  test("matches the Python round-trip byte for byte", () => {
    // If this fails the orchestrator's verify() will reject valid ops-console
    // signatures. Most likely culprit: a future refactor switching encoding.
    expect(signActor("alice@clinic", KEY)).toBe(
      pyMatchingSig("alice@clinic", KEY),
    );
  });

  test("is deterministic across calls", () => {
    expect(signActor("alice", KEY)).toBe(signActor("alice", KEY));
  });

  test("differs across actors and keys", () => {
    expect(signActor("alice", KEY)).not.toBe(signActor("bob", KEY));
    expect(signActor("alice", KEY)).not.toBe(
      signActor("alice", "other-key"),
    );
  });

  test("handles unicode actor strings", () => {
    const actor = "अलिस@clinic.in";
    expect(signActor(actor, KEY)).toBe(pyMatchingSig(actor, KEY));
  });

  test("throws on empty key", () => {
    expect(() => signActor("alice", "")).toThrow();
  });
});

describe("actorHeaders", () => {
  test("returns empty when no actor supplied", () => {
    expect(actorHeaders(undefined)).toEqual({});
    expect(actorHeaders("")).toEqual({});
  });

  test("returns only X-Ops-Actor when signing key is unset", () => {
    const original = process.env.OPS_ACTOR_SIGNING_KEY;
    delete process.env.OPS_ACTOR_SIGNING_KEY;
    try {
      expect(actorHeaders("alice")).toEqual({ "x-ops-actor": "alice" });
    } finally {
      if (original !== undefined) {
        process.env.OPS_ACTOR_SIGNING_KEY = original;
      }
    }
  });

  test("includes signature when signing key is set", () => {
    const original = process.env.OPS_ACTOR_SIGNING_KEY;
    process.env.OPS_ACTOR_SIGNING_KEY = KEY;
    try {
      const headers = actorHeaders("alice@clinic");
      expect(headers["x-ops-actor"]).toBe("alice@clinic");
      expect(headers["x-ops-actor-signature"]).toBe(
        signActor("alice@clinic", KEY),
      );
    } finally {
      if (original !== undefined) {
        process.env.OPS_ACTOR_SIGNING_KEY = original;
      } else {
        delete process.env.OPS_ACTOR_SIGNING_KEY;
      }
    }
  });
});
