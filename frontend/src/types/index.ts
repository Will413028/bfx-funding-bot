// ── API Response Wrappers ──

export interface ApiResponse<T> {
  data: T;
}

export interface ApiErrorResponse {
  error: {
    code: string;
    message: string;
  };
}

export interface CursorPagination {
  nextCursor?: string;
  hasMore: boolean;
}

// ── User ──

export interface User {
  id: string;
  email: string;
  status: "active" | "suspended";
  plan: "free" | "starter" | "pro" | "enterprise";
  createdAt: string;
  updatedAt: string;
}

// ── API Key ──

export interface ApiKey {
  id: string;
  label: string;
  apiKey: string;
  apiSecret: string; // always "****" (masked by backend)
  exchangeStatus: string; // "verified" | "unverified" | "failed"
  lastVerifyError?: string | null; // reason the last verify failed (when exchangeStatus is "failed")
  createdAt: string;
  fundingBalance?: {
    currency: string;
    balance: number;
    available: number;
  };
}

export interface VerifyResult {
  status: string; // "verified" | "failed"
  error?: string;
  fundingBalance?: {
    currency: string;
    balance: number;
    available: number;
  };
}

// ── Strategy Config ──

export interface AmountConfig {
  min: number;
  max: number;
}

export interface RateConfig {
  min: number;
  max: number;
}

export interface PeriodConfig {
  min: number;
  max: number;
}

export interface StrategyConfig {
  currency: string;
  amount: AmountConfig;
  rate: RateConfig;
  period: PeriodConfig;
  autoRenew: boolean;
}

export interface UserConfig {
  id: string;
  userId: string;
  config: StrategyConfig;
  createdAt: string;
  updatedAt: string;
}

// ── SP4 Projections (operator console read models) ──

/** offer_claims FSM states (backend RegistryState). */
export type OfferClaimState = "pending" | "claimed" | "released" | "failed";

/** event_log event types (backend serialization registry). */
export type ExecutionEventType =
  | "RESERVATION_INTENT"
  | "RESERVATION_CLAIMED"
  | "RESERVATION_FAILED"
  | "ORDER_FILL"
  | "RESERVATION_RELEASED"
  | "CREDIT_CLOSED";

/** GET /positions — per-symbol position_state ledger projection. */
export interface Position {
  symbol: string;
  /** Decimal string (USDT). */
  reserved: string;
  /** Decimal string (USDT). */
  realized: string;
  nCredits: number | null;
  lastUpdatedMs: number;
  /** Epoch ms of the last reconcile checkpoint; null if never reconciled. */
  lastReconciledAt: number | null;
  lastEventSeq: number;
}

/** GET /offers — cid-keyed offer claim (default: pending/claimed only). */
export interface OfferClaim {
  cid: number;
  venueOfferId: string | null;
  state: OfferClaimState;
  symbol: string;
  /** Decimal string (USDT). */
  sizeUsdt: string;
  occurredAtMs: number;
  lastUpdatedMs: number;
}

/** GET /executions — one event_log row, event_seq descending. */
export interface ExecutionEvent {
  eventSeq: number;
  eventType: ExecutionEventType;
  occurredAtMs: number;
  symbol: string | null;
  venueOfferId: string | null;
  cid: number | null;
  /** Decimal string (USDT); null when the payload carries no amount. */
  amount: string | null;
  /** Daily rate (e.g. 0.00017); null when the payload carries no rate. */
  rate: number | null;
}

// ── Billing ──

export interface BillingRecord {
  id: string;
  userId: string;
  periodStart: string;
  periodEnd: string;
  plan: string;
  amount: number;
  currency: string;
  status: "pending" | "paid" | "overdue" | "waived";
  paidAt?: string;
  createdAt: string;
}

export interface BillingListResponse {
  data: BillingRecord[];
  pagination: CursorPagination;
}

// ── Execution ──

export interface ExecutionRecord {
  id: string;
  userId: string;
  action: "place" | "cancel" | "filled" | "renew";
  currency: string;
  amount: number;
  rate: number;
  period: number;
  offerId?: number;
  status: string;
  errorMessage?: string;
  createdAt: string;
}

export interface ExecutionListResponse {
  data: ExecutionRecord[];
  pagination: CursorPagination;
}

// ── Attribution ──

export interface WeeklyAttributionPoint {
  cell: string;
  weekStartMs: number;
  weekEndMs: number;
  nFills: number;
  grossInterestUsdt: string;
  netInterestUsdt: string;
  capitalDays: string;
  realizedAprNetPct: string | null;
  baselineCloseAprNetPct: string | null;
  baselineFrrAprNetPct: string | null;
  baselineFrrUtilAprNetPct: string | null;
}
