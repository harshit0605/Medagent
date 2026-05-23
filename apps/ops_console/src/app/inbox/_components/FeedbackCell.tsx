"use client";

/**
 * Per-inbox-row bot-reply quality feedback.
 *
 * Renders a thumbs-up / thumbs-down pair. Click immediately
 * POSTs the rating; the local state updates so the operator
 * sees the change without a page reload. Errors render inline
 * (small) so the operator can retry without losing context.
 *
 * For thumbs-down we don't (yet) prompt for a note inline —
 * keep the click cheap. A v2 could open a popover for a quick
 * "what was wrong" capture.
 */

import { useState, useTransition } from "react";

import { orchestrator } from "@/lib/backend";

type Props = {
  classificationId: number;
  initialRating: number | null;
};

const DEFAULT_ACTOR = "ops_console";

export function FeedbackCell({ classificationId, initialRating }: Props) {
  const [rating, setRating] = useState<number | null>(initialRating);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const submit = (next: -1 | 1) => {
    setError(null);
    const previous = rating;
    // Optimistic update so the click feels instant; we'll roll
    // back on error.
    setRating(next);
    startTransition(async () => {
      try {
        await orchestrator.setInboxFeedback(classificationId, {
          rating: next,
          actor: DEFAULT_ACTOR,
        });
      } catch (err) {
        setRating(previous);
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  };

  const clear = () => {
    setError(null);
    const previous = rating;
    setRating(null);
    startTransition(async () => {
      try {
        await orchestrator.clearInboxFeedback(classificationId);
      } catch (err) {
        setRating(previous);
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  };

  const baseBtn =
    "px-1.5 py-0.5 text-xs rounded border transition-colors disabled:opacity-50";

  return (
    <div className="flex flex-col items-start gap-0.5">
      <div className="flex gap-1">
        <button
          type="button"
          disabled={pending}
          onClick={() => (rating === 1 ? clear() : submit(1))}
          className={
            baseBtn +
            " " +
            (rating === 1
              ? "bg-emerald-600 border-emerald-700 text-white hover:bg-emerald-700"
              : "border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-400 hover:bg-emerald-50 dark:hover:bg-emerald-950/40 hover:text-emerald-700 dark:hover:text-emerald-300")
          }
          title={
            rating === 1
              ? "You marked this reply good. Click to clear."
              : "Mark this reply good"
          }
          aria-pressed={rating === 1}
        >
          👍
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => (rating === -1 ? clear() : submit(-1))}
          className={
            baseBtn +
            " " +
            (rating === -1
              ? "bg-red-600 border-red-700 text-white hover:bg-red-700"
              : "border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-400 hover:bg-red-50 dark:hover:bg-red-950/40 hover:text-red-700 dark:hover:text-red-300")
          }
          title={
            rating === -1
              ? "You marked this reply bad. Click to clear."
              : "Mark this reply bad — bot got it wrong"
          }
          aria-pressed={rating === -1}
        >
          👎
        </button>
      </div>
      {error ? (
        <div className="text-[10px] text-red-600 dark:text-red-400">
          {error}
        </div>
      ) : null}
    </div>
  );
}
