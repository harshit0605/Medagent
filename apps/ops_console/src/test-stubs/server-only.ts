/**
 * Noop stub for the Next.js ``server-only`` import.
 *
 * In a real Next.js build, importing ``server-only`` from a client component
 * triggers a build-time error — the entire module tree it's reachable from
 * is forced server-side. Under vitest we run in a Node environment where the
 * guard is meaningless, so this stub exports nothing and lets the test
 * import the module under test without resolution errors.
 *
 * Aliased via ``vitest.config.ts``.
 */

export {};
