/**
 * GET /api/google/oauth/callback?code=...&state=...
 *
 * Google redirects the doctor's browser here after consent. We:
 *   1. Validate `state` matches the cookie nonce we set in /start.
 *   2. Extract doctor_id from the state.
 *   3. POST {code, redirect_uri} to the orchestrator's
 *      /doctors/{id}/oauth/callback endpoint, which exchanges the code
 *      server-side and persists the encrypted refresh token.
 *   4. Redirect the doctor back to /doctors with a success/error toast.
 */

import { NextRequest, NextResponse } from "next/server";

import { STATE_COOKIE_NAME, readOAuthEnv } from "@/lib/google-oauth";

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? "http://localhost:8002";
const ORCHESTRATOR_API_KEY = process.env.ORCHESTRATOR_API_KEY;

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function back(
  request: NextRequest,
  status: "ok" | "error",
  detail?: string,
): NextResponse {
  // NextResponse.redirect requires an ABSOLUTE URL — build it from the
  // incoming request's origin so it works on localhost, cloudflared, or
  // wherever the ops console is hosted.
  const target = new URL("/doctors", request.nextUrl.origin);
  target.searchParams.set("oauth", status);
  if (detail) target.searchParams.set("detail", detail);
  return NextResponse.redirect(target, { status: 302 });
}

export async function GET(request: NextRequest) {
  const env = readOAuthEnv();
  if (!env) return back(request, "error", "google_oauth_not_configured");

  const errorParam = request.nextUrl.searchParams.get("error");
  if (errorParam) return back(request, "error", errorParam);

  const code = request.nextUrl.searchParams.get("code");
  const state = request.nextUrl.searchParams.get("state");
  if (!code || !state) return back(request, "error", "missing_code_or_state");

  // state = `${doctor_id}:${nonce}`
  const [doctorIdRaw, nonceFromState] = state.split(":");
  const doctorId = Number(doctorIdRaw);
  if (!Number.isFinite(doctorId) || !nonceFromState) {
    return back(request, "error", "malformed_state");
  }

  const cookie = request.cookies.get(STATE_COOKIE_NAME);
  if (!cookie || cookie.value !== nonceFromState) {
    return back(request, "error", "state_mismatch");
  }

  // Forward the code to the orchestrator for the server-to-server token exchange.
  let res: Response;
  try {
    const headers: Record<string, string> = {
      "content-type": "application/json",
    };
    // Authenticate the server-to-server call when the orchestrator enforces
    // a shared secret (it fails closed by default). Without this the exchange
    // would 401 and every OAuth connect would fail with orchestrator_401.
    if (ORCHESTRATOR_API_KEY) headers["x-api-key"] = ORCHESTRATOR_API_KEY;
    res = await fetch(`${ORCHESTRATOR_URL}/doctors/${doctorId}/oauth/callback`, {
      method: "POST",
      headers,
      body: JSON.stringify({ code, redirect_uri: env.redirectUri }),
      cache: "no-store",
    });
  } catch (err) {
    console.error("[google-oauth] orchestrator unreachable:", err);
    return back(request, "error", "orchestrator_unreachable");
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    console.error(
      "[google-oauth] orchestrator callback failed: %s %s",
      res.status,
      text,
    );
    // Surface the orchestrator's detail (which now carries Google's actual
    // `error_description`) into the URL fragment for ops to read.
    let detail = `orchestrator_${res.status}`;
    try {
      const body = JSON.parse(text);
      if (typeof body?.detail === "string") {
        detail = body.detail.slice(0, 240);
      }
    } catch {
      // not JSON — keep the generic detail
    }
    return back(request, "error", detail);
  }

  const response = back(request, "ok", `doctor_${doctorId}`);
  // Best-effort: clear the nonce cookie so it can't be replayed.
  response.cookies.set({
    name: STATE_COOKIE_NAME,
    value: "",
    path: "/",
    maxAge: 0,
  });
  return response;
}
