import type { NextConfig } from "next";

/**
 * Security headers applied to every response.
 *
 * - CSP: `default-src 'self'` baseline. `style-src` allows `'unsafe-inline'`
 *   because Next.js injects critical CSS inline at build time and the React
 *   Compiler may inject style attributes; tightening this would require a
 *   nonce-based proxy (`proxy.ts`) which forces dynamic rendering everywhere.
 *   Revisit when we have an appetite for the proxy refactor.
 * - `frame-ancestors 'none'` blocks the ops console from being embedded in an
 *   iframe (clickjacking defense for DSAR/erasure triggers).
 * - HSTS: 1 year + preload. Coolify+Traefik already redirects HTTP→HTTPS at
 *   the edge, so this is belt-and-suspenders for direct-IP access.
 * - `X-Content-Type-Options: nosniff` blocks MIME-sniffing-based XSS.
 * - `Referrer-Policy: strict-origin-when-cross-origin` prevents full-URL
 *   leakage to external links (DSAR/audit URLs may carry patient IDs).
 * - `Permissions-Policy` disables browser APIs the console doesn't need.
 *
 * For app-router setups, `next.config.ts` `headers()` runs at every request
 * and applies to all routes (`/:path*`) including API routes and static
 * assets. No middleware/proxy required.
 */
const securityHeaders = [
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      "script-src 'self'",
      // Next + Tailwind require inline styles for critical CSS injection.
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      // No remote fetches except same-origin (the ops-console talks to the
      // orchestrator + gateway server-side, never from the browser).
      "connect-src 'self'",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "upgrade-insecure-requests",
    ].join("; "),
  },
  {
    key: "Strict-Transport-Security",
    value: "max-age=31536000; includeSubDomains; preload",
  },
  {
    key: "X-Content-Type-Options",
    value: "nosniff",
  },
  {
    key: "X-Frame-Options",
    value: "DENY",
  },
  {
    key: "Referrer-Policy",
    value: "strict-origin-when-cross-origin",
  },
  {
    key: "Permissions-Policy",
    value: [
      "camera=()",
      "microphone=()",
      "geolocation=()",
      "payment=()",
      "usb=()",
    ].join(", "),
  },
];

const nextConfig: NextConfig = {
  reactCompiler: true,
  // Drop the `X-Powered-By: Next.js` header — gives attackers no version hint.
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
