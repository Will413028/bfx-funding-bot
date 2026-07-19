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

export const billingKeys = {
  all: ["billing"] as const,
  list: (params?: Record<string, unknown>) =>
    [...billingKeys.all, "list", params] as const,
  plan: () => [...billingKeys.all, "plan"] as const,
};

export const executionKeys = {
  all: ["executions"] as const,
  list: (params?: Record<string, unknown>) =>
    [...executionKeys.all, "list", params] as const,
  /** SP4 event_log projection (before-cursor infinite list). */
  events: () => [...executionKeys.all, "events"] as const,
};

export const attributionKeys = {
  all: ["attribution"] as const,
  weekly: () => [...attributionKeys.all, "weekly"] as const,
};
