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

/**
 * Account credential lifecycle as returned by the account-scoped API.
 * `unverified` is retained for legacy rows during the staged contract
 * migration; new credentials start in `pending` and are fail-closed until a
 * successful permission check promotes them to `verified`.
 */
export type ApiKeyStatus =
  | "pending"
  | "verified"
  | "unverified"
  | "failed"
  | "revoked"
  | "retired";

export interface ApiKey {
  id: string;
  exchangeAccountId: string;
  label: string;
  apiKey: string;
  apiSecret: string; // always "****" (masked by backend)
  /** Account credential lifecycle/verification status. */
  status: ApiKeyStatus;
  /** Legacy response field kept only while the contract migration is staged. */
  exchangeStatus?: string;
  verifiedAt?: string | null;
  lastVerifyError?: string | null; // reason the last verify failed (when exchangeStatus is "failed")
  createdAt: string;
  fundingBalance?: {
    currency: string;
    balance: number;
    available: number;
  };
}

export interface ExchangeAccount {
  exchangeAccountId: string;
  venue: string;
  label: string;
  lifecycleStatus: "active" | "halted";
  role: "owner" | "operator" | "viewer";
}

export interface VerifyResult {
  status: ApiKeyStatus;
  error?: string | null;
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
  exchangeAccountId: string;
  config: StrategyConfig;
  revision: number;
  source: string;
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
  lastReconciledAtMs: number | null;
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

/**
 * GET /executions pagination (contract v2) — server envelope replaces the
 * old "full page => more" client heuristic.
 */
export interface ExecutionEventsPagination {
  hasMore: boolean;
  /** event_seq cursor for the next page; null when the log is exhausted. */
  nextBefore: number | null;
}

/** GET /executions — full response envelope. */
export interface ExecutionEventsResponse {
  data: ExecutionEvent[];
  pagination: ExecutionEventsPagination;
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

// ── Public proof page (percent-only, no absolute $ — no auth required) ──

/** GET /public/proof-summary — one week of the blended public series. */
export interface PublicProofWeek {
  weekStartMs: number;
  realizedAprNetPct: string | null;
  baselineFrrUtilAprNetPct: string | null;
}

/** GET /public/proof-summary — full response envelope. */
export interface PublicProofSummary {
  weeks: PublicProofWeek[];
  asOf: string;
}
