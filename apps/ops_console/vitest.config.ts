/**
 * Vitest config — node environment, picks up *.test.ts files anywhere under
 * src/. Scoped to the ops console workspace; the Python pytest suite handles
 * everything below ``services/``.
 *
 * No JSDOM today: the only currently-tested code is server-side utilities
 * (HMAC signing, signature verification). When React component tests land
 * we'll add ``environment: "jsdom"`` + @testing-library/react.
 */

import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      // ``import "server-only"`` is a Next.js build-time guard with no
      // npm-installed implementation. Under vitest the import resolves to
      // a noop so the tested modules import cleanly. The build-time guard
      // still works in the real Next.js compile pass.
      "server-only": resolve(__dirname, "src/test-stubs/server-only.ts"),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
