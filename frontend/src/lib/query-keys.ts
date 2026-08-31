export const userKeys = {
  all: ["user"] as const,
  me: () => [...userKeys.all, "me"] as const,
};

export const exchangeAccountKeys = {
  all: ["exchange-accounts"] as const,
  list: () => [...exchangeAccountKeys.all, "list"] as const,
};

export const apiKeyKeys = {
  all: ["api-keys"] as const,
  list: (exchangeAccountId: string | undefined) =>
    [...apiKeyKeys.all, exchangeAccountId, "list"] as const,
};

export const configKeys = {
  all: ["configs"] as const,
  current: (exchangeAccountId: string | undefined) =>
    [...configKeys.all, exchangeAccountId, "current"] as const,
};

export const positionKeys = {
  all: ["positions"] as const,
  list: (exchangeAccountId: string | undefined) =>
    [...positionKeys.all, exchangeAccountId, "list"] as const,
};

export const offerKeys = {
  all: ["offers"] as const,
  list: (exchangeAccountId: string | undefined, state?: string) =>
    [...offerKeys.all, exchangeAccountId, "list", state] as const,
};

export const executionKeys = {
  all: ["executions"] as const,
  /** SP4 event_log projection (before-cursor infinite list, per filter). */
  events: (exchangeAccountId: string | undefined, eventType?: string) =>
    [
      ...executionKeys.all,
      exchangeAccountId,
      "events",
      eventType ?? "all",
    ] as const,
};

export const attributionKeys = {
  all: ["attribution"] as const,
  weekly: (exchangeAccountId: string | undefined) =>
    [...attributionKeys.all, exchangeAccountId, "weekly"] as const,
};
