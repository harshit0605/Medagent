/**
 * GET /api/google/oauth/start?doctor_id=N
 *
 * Initiates the per-doctor Google OAuth flow. Redirects the doctor's browser
 * to Google's consent screen. We embed `doctor_id` in the OAuth `state`
 * parameter (signed-ish via random opaque cookie) so the callback knows
 * which doctor's tokens to persist.
 */

import { randomBytes } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

import {
  buildAuthorizationUrl,
  readOAuthEnv,
  stateCookie,
} from "@/lib/google-oauth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const env = readOAuthEnv();
  if (!env) {
    return NextResponse.json(
      {
        error: "google_oauth_not_configured",
        missing_env: ["GOOGLE_CLIENT_ID", "GOOGLE_REDIRECT_URI"].filter(
          (k) => !process.env[k],
        ),
      },
      { status: 503 },
    );
  }

  const doctorId = request.nextUrl.searchParams.get("doctor_id");
  if (!doctorId || !/^\d+$/.test(doctorId)) {
    return NextResponse.json(
      { error: "doctor_id is required (numeric)" },
      { status: 400 },
    );
  }

  // The state ties the consent screen to this specific browser session AND
  // carries the doctor_id over to the callback. Two pieces:
  //   - `nonce`: random opaque, set as cookie + included in state, compared on callback
  //   - `doctor_id`: encoded into state so the callback can persist tokens for the right doctor
  const nonce = randomBytes(24).toString("hex");
  const state = `${doctorId}:${nonce}`;

  const url = buildAuthorizationUrl({ ...env, state });

  const response = NextResponse.redirect(url, { status: 302 });
  const cookie = stateCookie(nonce);
  response.cookies.set(cookie);
  return response;
}
