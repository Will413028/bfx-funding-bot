"use client";

import { parseAsStringLiteral, useQueryState } from "nuqs";
import type { ExecutionEventType } from "@/types";

/** All event_log types, in lifecycle order (filter chip order). */
export const EVENT_TYPE_OPTIONS = [
  "RESERVATION_INTENT",
  "RESERVATION_CLAIMED",
  "RESERVATION_FAILED",
  "ORDER_FILL",
  "RESERVATION_RELEASED",
  "CREDIT_CLOSED",
  "UNCERTAINTY_BOUND_TO_VENUE_OFFER",
  "UNCERTAINTY_MARKED_NOT_ACCEPTED",
  "UNCERTAINTY_MANUALLY_RESOLVED",
] as const satisfies readonly ExecutionEventType[];

/**
 * URL-backed event-type filter (`?eventType=ORDER_FILL`).
 * `null` = all events; unknown values in the URL parse to `null`.
 */
export function useEventTypeFilter() {
  return useQueryState("eventType", parseAsStringLiteral(EVENT_TYPE_OPTIONS));
}
