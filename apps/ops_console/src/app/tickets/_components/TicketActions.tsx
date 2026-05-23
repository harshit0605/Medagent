"use client";

import { useState, useTransition } from "react";

import { ackTicketAction, resolveTicketAction } from "../_actions";

type Props = {
  ticketId: string;
  status: "open" | "acknowledged" | "resolved";
};

const DEFAULT_ACTOR = "ops_console";

export function TicketActions({ ticketId, status }: Props) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [resolveNotes, setResolveNotes] = useState("");

  const onAck = () => {
    setError(null);
    startTransition(async () => {
      try {
        await ackTicketAction(ticketId, DEFAULT_ACTOR);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  };

  const onResolve = () => {
    setError(null);
    startTransition(async () => {
      try {
        await resolveTicketAction(ticketId, DEFAULT_ACTOR, resolveNotes || undefined);
        setResolveNotes("");
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  };

  if (status === "resolved") {
    return <span className="text-xs text-zinc-400">resolved</span>;
  }

  return (
    <div className="flex flex-col gap-2 items-end">
      <div className="flex gap-2">
        {status === "open" ? (
          <button
            type="button"
            onClick={onAck}
            disabled={pending}
            className="px-3 py-1 text-xs rounded border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50"
          >
            {pending ? "..." : "Ack"}
          </button>
        ) : null}
        <button
          type="button"
          onClick={onResolve}
          disabled={pending}
          className="px-3 py-1 text-xs rounded bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-50"
        >
          {pending ? "..." : "Resolve"}
        </button>
      </div>
      <input
        type="text"
        value={resolveNotes}
        onChange={(e) => setResolveNotes(e.target.value)}
        placeholder="resolve note (optional)"
        className="text-xs px-2 py-1 rounded border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 w-48"
      />
      {error ? <span className="text-xs text-red-600 max-w-xs text-right">{error}</span> : null}
    </div>
  );
}
