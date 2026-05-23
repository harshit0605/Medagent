/**
 * Google OAuth helpers (server-only).
 *
 * Flow:
 *   /api/google/oauth/start?doctor_id=N   →  302 to Google consent
 *   Google                                 →  302 to /api/google/oauth/callback?code=…&state=…
 *   /api/google/oauth/callback             →  POST {code, redirect_uri} to orchestrator
 *                                            /doctors/{id}/oauth/callback (which exchanges
 *                                            the code server-side and persists the
 *                                            encrypted refresh token)
 */

const SCOPES = [
  // Read events + free/busy. Required by /freeBusy — `calendar.events` alone
  // covers writes but Google enforces a separate read scope for freeBusy.
  "https://www.googleapis.com/auth/calendar.readonly",
  // Create / update / delete events on the doctor's calendars.
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/userinfo.email",
  "openid",
];

export type OAuthEnv = {
  clientId: string;
  redirectUri: string;
};

export function readOAuthEnv(): OAuthEnv | null {
  const clientId = process.env.GOOGLE_CLIENT_ID;
  const redirectUri = process.env.GOOGLE_REDIRECT_URI;
  if (!clientId || !redirectUri) return null;
  return { clientId, redirectUri };
}

/** Build the Google consent URL the doctor's browser is sent to. */
export function buildAuthorizationUrl({
  clientId,
  redirectUri,
  state,
}: OAuthEnv & { state: string }): string {
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    // `offline` is what makes Google issue a refresh_token — without it we
    // only get a 1-hour access token and can't keep the doctor connected.
    access_type: "offline",
    // `consent` forces Google to RE-prompt even if the doctor already
    // authorized us once — guarantees a fresh refresh_token (Google only
    // returns one on first consent unless explicitly re-prompted).
    prompt: "consent",
    include_granted_scopes: "true",
    scope: SCOPES.join(" "),
    state,
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;
}

const STATE_COOKIE = "medagent_oauth_state";
const STATE_TTL_SECONDS = 15 * 60;

export function stateCookie(value: string) {
  return {
    name: STATE_COOKIE,
    value,
    httpOnly: true,
    sameSite: "lax" as const,
    path: "/",
    maxAge: STATE_TTL_SECONDS,
    secure: process.env.NODE_ENV === "production",
  };
}

export const STATE_COOKIE_NAME = STATE_COOKIE;
