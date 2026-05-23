"use client";

import { useState, useTransition } from "react";

import type { DoctorAvailability } from "@/lib/backend";

import { checkAvailabilityAction } from "../_availability_action";

type Props = { doctorId: number; timezone: string };

function defaultStart(): string {
  // Tomorrow 09:00 in the local browser TZ — formatted for `<input type="datetime-local">`.
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return toLocalInput(d);
}

function defaultEnd(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(17, 0, 0, 0);
  return toLocalInput(d);
}

function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function toIso(local: string): string {
  return new Date(local).toISOString();
}

function fmt(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString();
}

export function AvailabilityChecker({ doctorId, timezone }: Props) {
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<DoctorAvailability | null>(null);
  const [error, setError] = useState<string | null>(null);

  const onCheck = (formData: FormData) => {
    setError(null);
    startTransition(async () => {
      try {
        const data = await checkAvailabilityAction({
          doctorId,
          start: toIso(String(formData.get("start"))),
          end: toIso(String(formData.get("end"))),
          duration_minutes: Number(formData.get("duration") || 30),
        });
        setResult(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setResult(null);
      }
    });
  };

  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 mt-3">
      <div className="text-xs uppercase tracking-wide text-zinc-500 mb-2">
        Check availability ({timezone})
      </div>
      <form action={onCheck} className="flex flex-wrap items-end gap-2 text-xs">
        <label className="flex flex-col gap-1">
          Start
          <input
            type="datetime-local"
            name="start"
            defaultValue={defaultStart()}
            required
            className="px-2 py-1 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
          />
        </label>
        <label className="flex flex-col gap-1">
          End
          <input
            type="datetime-local"
            name="end"
            defaultValue={defaultEnd()}
            required
            className="px-2 py-1 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
          />
        </label>
        <label className="flex flex-col gap-1">
          Duration (min)
          <input
            type="number"
            name="duration"
            defaultValue={30}
            min={5}
            max={480}
            className="w-20 px-2 py-1 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900"
          />
        </label>
        <button
          type="submit"
          disabled={pending}
          className="px-3 py-1.5 rounded bg-zinc-900 text-white hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {pending ? "Checking..." : "Find slots"}
        </button>
      </form>

      {error ? (
        <div className="mt-3 text-xs text-red-700 dark:text-red-300">{error}</div>
      ) : null}

      {result ? (
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
          <div>
            <div className="font-medium mb-1">Free slots ({result.free.length})</div>
            <ul className="space-y-0.5 max-h-48 overflow-auto pr-1">
              {result.free.length === 0 ? (
                <li className="text-zinc-500">none</li>
              ) : (
                result.free.map((s) => (
                  <li key={s.start} className="font-mono">
                    {fmt(s.start)} → {fmt(s.end)}
                  </li>
                ))
              )}
            </ul>
          </div>
          <div>
            <div className="font-medium mb-1">Busy ({result.busy.length})</div>
            <ul className="space-y-0.5 max-h-48 overflow-auto pr-1">
              {result.busy.length === 0 ? (
                <li className="text-zinc-500">none</li>
              ) : (
                result.busy.map((s) => (
                  <li key={s.start} className="font-mono text-zinc-500">
                    {fmt(s.start)} → {fmt(s.end)}
                  </li>
                ))
              )}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
