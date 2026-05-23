/**
 * WhatsApp inbound webhook — Meta → us.
 *
 *   GET  /api/whatsapp/webhook   Meta verify-token handshake (one-time setup).
 *   POST /api/whatsapp/webhook   Signed inbound (X-Hub-Signature-256).
 *
 * The chat-sdk WhatsApp adapter (`@chat-adapter/whatsapp`) handles signature
 * verification + payload parsing + dispatching to our `onDirectMessage`
 * handler (registered in src/lib/whatsapp-bot.ts), which forwards each
 * message to the Python orchestrator and routes the agent's reply back
 * through the Python gateway.
 *
 * Status updates (sent / delivered / read / failed) are NOT exposed as a
 * chat-sdk event, so we parse them out of the raw body in parallel and POST
 * them to the Python gateway's `/internal/whatsapp/status` endpoint. This
 * means we read the body once, replay it into chat-sdk via a cloned Request,
 * and parse statuses ourselves on the same bytes.
 *
 * Background-vs-inline trade-off:
 *   - WHATSAPP_WEBHOOK_ASYNC=1 → use Next.js `after()` so the 200 ack returns
 *     fast; processing keeps running in the background. Best on Vercel.
 *   - WHATSAPP_WEBHOOK_ASYNC=0 (default) → await processing inline before
 *     returning 200. Slower (~1–3s) but guaranteed completion in any
 *     runtime — including local `bun run dev` where `after()` did not
 *     reliably keep the in-flight fetches alive in our smoke tests.
 */

import { NextRequest, NextResponse } from "next/server";
import { after } from "next/server";

import { forwardStatuses } from "@/lib/whatsapp-statuses";
import { getBot, missingWhatsAppEnv } from "@/lib/whatsapp-bot";
import { verifyWhatsAppSignature } from "@/lib/whatsapp-signature";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function notConfiguredResponse(): NextResponse {
  return NextResponse.json(
    {
      error: "whatsapp_not_configured",
      missing_env: missingWhatsAppEnv(),
    },
    { status: 503 },
  );
}

function asyncMode(): boolean {
  return (process.env.WHATSAPP_WEBHOOK_ASYNC ?? "0") === "1";
}

export async function GET(request: NextRequest) {
  const bot = getBot();
  if (!bot) return notConfiguredResponse();
  return bot.webhooks.whatsapp(request);
}

export async function POST(request: NextRequest) {
  const bot = getBot();
  if (!bot) return notConfiguredResponse();

  // Read raw body once; we replay it into chat-sdk and parse statuses ourselves.
  const rawBody = await request.text();

  // Defense-in-depth: verify the X-Hub-Signature-256 BEFORE we
  // touch the body. The chat-sdk adapter verifies on its path,
  // but `forwardStatuses` parses statuses ourselves and would
  // run against forged bytes if we kicked it off in parallel.
  // Failing fast here also avoids spawning fetches into the
  // gateway for unsigned payloads.
  const signatureValid = verifyWhatsAppSignature(
    rawBody,
    request.headers.get("x-hub-signature-256"),
    process.env.WHATSAPP_APP_SECRET,
  );
  if (!signatureValid) {
    console.warn(
      "[whatsapp] rejecting webhook POST: signature mismatch or missing secret",
    );
    return NextResponse.json(
      { error: "invalid_signature" },
      { status: 401 },
    );
  }

  const cloned = new Request(request.url, {
    method: "POST",
    headers: request.headers,
    body: rawBody,
  });

  // Parse statuses up front (cheap) and queue the forward — we'll either
  // await it inline or hand it to `after()` based on the env flag. Safe
  // to do here because the signature has already been validated above.
  const statusesPromise = forwardStatuses(rawBody);

  let pending: Promise<unknown> | undefined;
  const response = await bot.webhooks.whatsapp(cloned as unknown as NextRequest, {
    waitUntil: (task) => {
      pending = task;
    },
  });

  const allWork = Promise.all([
    pending ?? Promise.resolve(),
    statusesPromise,
  ]);

  if (asyncMode()) {
    after(async () => {
      try {
        await allWork;
      } catch (err) {
        console.error("[whatsapp] background processing failed:", err);
      }
    });
  } else {
    try {
      await allWork;
    } catch (err) {
      console.error("[whatsapp] inline processing failed:", err);
    }
  }
  return response;
}
