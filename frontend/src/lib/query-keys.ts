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

export const dashboardKeys = {
  all: ["dashboard"] as const,
  summary: () => [...dashboardKeys.all, "summary"] as const,
};

export const earningsKeys = {
  all: ["earnings"] as const,
  summary: () => [...earningsKeys.all, "summary"] as const,
  history: (days?: number) => [...earningsKeys.all, "history", days] as const,
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
};

export const attributionKeys = {
  all: ["attribution"] as const,
  weekly: () => [...attributionKeys.all, "weekly"] as const,
};
