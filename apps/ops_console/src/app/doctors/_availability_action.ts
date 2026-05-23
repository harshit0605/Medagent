"use server";

import { orchestrator, type DoctorAvailability } from "@/lib/backend";

export async function checkAvailabilityAction(args: {
  doctorId: number;
  start: string;
  end: string;
  duration_minutes: number;
}): Promise<DoctorAvailability> {
  return orchestrator.doctorAvailability(args.doctorId, {
    start: args.start,
    end: args.end,
    duration_minutes: args.duration_minutes,
  });
}
