import { RouteTesterForm } from "./_components/RouteTesterForm";

export const dynamic = "force-dynamic";

export default function RouteTesterPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Route tester</h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Send an inbound message through the orchestrator&apos;s{" "}
          <code className="font-mono text-xs">/route</code> endpoint and inspect the agent
          decision — intent, policy reasons, risk level, escalation, and the composed reply.
        </p>
      </div>
      <RouteTesterForm />
    </div>
  );
}
