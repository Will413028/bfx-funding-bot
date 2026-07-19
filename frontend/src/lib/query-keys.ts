export const userKeys = {
  all: ["user"] as const,
  me: () => [...userKeys.all, "me"] as const,
};

export const apiKeyKeys = {
  all: ["api-keys"] as const,
  list: () => [...apiKeyKeys.all, "list"] as const,
};

export const configKeys = {
  all: ["configs"] as const,
  current: () => [...configKeys.all, "current"] as const,
};

export const positionKeys = {
  all: ["positions"] as const,
  list: () => [...positionKeys.all, "list"] as const,
};

export const offerKeys = {
  all: ["offers"] as const,
  list: (state?: string) => [...offerKeys.all, "list", state] as const,
};

export const executionKeys = {
  all: ["executions"] as const,
  /** SP4 event_log projection (before-cursor infinite list, per filter). */
  events: (eventType?: string) =>
    [...executionKeys.all, "events", eventType ?? "all"] as const,
};

export const attributionKeys = {
  all: ["attribution"] as const,
  weekly: () => [...attributionKeys.all, "weekly"] as const,
};
