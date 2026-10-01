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

// Canonical live authority: Decimal values stay strings throughout the UI.
export interface CapitalPolicy {
  enabled: boolean;
  reserve_amount: string;
  allocation_mode: "all_available";
  max_cell_fraction: string;
}

export interface CapitalBudget {
  spendable: string;
  cell_limit: string;
  cell_headroom: string;
  max_new_offer: string;
  reason: string | null;
}

export type CapitalStatus =
  | {
      capital_available: false;
      reason: string;
      policy_revision?: number;
      policy?: CapitalPolicy;
    }
  | {
      capital_available: true;
      policy_revision: number;
      policy_digest: string;
      basis_token: string;
      policy: CapitalPolicy;
      available_balance: string;
      unreflected_commitments: string;
      total_capital: string;
      spendable: string;
      unattributed_credit_exposure: string;
      cells: Record<string, CapitalBudget>;
    };

export interface FundingStatus {
  account_id: string;
  deployment_environment: string;
  halt: { halted: boolean; reason: string | null; sources: unknown };
  symbols: Record<string, CapitalStatus>;
  configured_cells: {
    symbol: string;
    cell: string;
    strategy: string;
    period: string;
  }[];
  dry_run: {
    account_id: string;
    deployment_environment: string;
    symbols: Record<
      string,
      {
        blocked_by: string | null;
        cells: Record<
          string,
          { would_submit: boolean; blocked_by: string | null }
        >;
      }
    >;
  };
}

// ── Trading control (lending envelope ADR 2026-09-25 D4: state, resume, kill) ──

export type TradingStateName = "ACTIVE" | "HALTED";
export type TradingCause = "operator" | "auto";

export interface TradingStateView {
  id: number;
  state: TradingStateName;
  cause: TradingCause;
  actor: string;
  reason: string;
  at_ms: number;
}

export type TradingControlAction = "resume" | "kill";
export type TradingControlRequestState =
  | "requested"
  | "applied"
  | "rejected"
  | "failed";

export interface TradingControlRequest {
  request_id: string;
  action: TradingControlAction;
  reason: string;
  requested_by: string;
  created_at_ms: number;
  state: TradingControlRequestState;
  processed_at_ms: number | null;
  outcome_reason: string | null;
  trading_state_id: number | null;
}

/** The latest venue cancel-all phase per currency for the HALTED in force. */
export interface CancelAllPhase {
  currency: string;
  phase: "requested" | "acknowledged" | "rejected" | "failed" | "skipped";
  detail: string | null;
  at_ms: number;
}

export interface BuildIdentity {
  backend_digest: string | null;
  source_revision: string | null;
}

export interface TradingControlOverview {
  trading_state: TradingStateView | null;
  cancel_all: CancelAllPhase[];
  running: BuildIdentity;
  latest_deployment: (BuildIdentity & { finished_at: string | null }) | null;
  requests: TradingControlRequest[];
  /** Every currency with an applied CapitalPolicy (its own request outbox). */
  currencies: CurrencyPolicy[];
}

export type CurrencyAction = "enable" | "disable";

export interface CurrencyRequest {
  request_id: string;
  symbol: string;
  action: CurrencyAction;
  reason: string;
  requested_by: string;
  created_at_ms: number;
  state: TradingControlRequestState;
  processed_at_ms: number | null;
  outcome_reason: string | null;
  policy_revision_id: string | null;
}

/** The terms every new offer must stay inside; decimals stay strings. */
export interface OfferEnvelope {
  min_period_days: number;
  max_period_days: number;
  max_open_offers: number;
  rate_floor_ratio: string;
  min_rate_apr: string;
}

export interface CurrencyPolicy {
  symbol: string;
  revision: number;
  /** Set when the applied policy cannot be read; the other fields are null. */
  policy_error: string | null;
  enabled: boolean | null;
  max_offer_amount: string | null;
  /** null: never set, and every offer for the currency is refused. */
  envelope: OfferEnvelope | null;
  /** Latest first. */
  requests: CurrencyRequest[];
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
  | "CREDIT_CLOSED"
  | "UNCERTAINTY_BOUND_TO_VENUE_OFFER"
  | "UNCERTAINTY_MARKED_NOT_ACCEPTED"
  | "UNCERTAINTY_MANUALLY_RESOLVED";

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

// ── Execution uncertainties (operator resolution) ──

export type UncertaintyKind =
  | "submit_outcome_unknown"
  | "unattributed_venue_offer"
  | "unsupported_venue_exposure";

export type UncertaintyState = "open" | "resolved";

/** Deliberately bounded values exposed by the backend operator DTO. */
export interface UncertaintyEvidenceSummary {
  outcomeReason?: string | null;
  observedAtMs?: number | null;
  candidateCount?: number | null;
  venueOfferId?: string | null;
  status?: string | null;
  coverage?: Record<string, boolean>;
}

export interface UncertaintyBlockedScope {
  exchangeAccountId: string;
  environment: string;
  symbol: string;
}

/** Latest server-derived evidence that can authorize an operator resolution. */
export interface UncertaintyResolutionContext {
  evidenceRef: string | null;
  queryStartedAtMs: number | null;
  queryFinishedAtMs: number | null;
  candidateCount: number | null;
  candidateVenueOfferIds: string[];
  unavailableReason: string | null;
}

/** Account/environment/symbol-scoped execution block. */
export interface Uncertainty {
  uncertaintyId: string;
  kind: UncertaintyKind;
  symbol: string;
  /** Decimal string (venue-native intended amount). */
  intendedAmount: string;
  state: UncertaintyState;
  evidenceSummary: UncertaintyEvidenceSummary;
  blockedScope: UncertaintyBlockedScope;
  resolutionContext: UncertaintyResolutionContext | null;
  resolvedByOperatorId?: string | null;
  resolutionReason?: string | null;
  /**
   * Newest operator adjudication for this row: `requested` while the account
   * daemon has yet to apply it, otherwise its outcome. The only status source.
   */
  resolutionRequest?: UncertaintyResolutionRequest | null;
}

export type UncertaintyResolutionRequestState =
  | "requested"
  | "applied"
  | "rejected"
  | "failed";

/**
 * One queued operator adjudication. The web API only records the request; the
 * account daemon applies it and reports the outcome here (ADR D4').
 */
export interface UncertaintyResolutionRequest {
  requestId: string;
  uncertaintyId: string;
  action: "bind_to_venue" | "mark_not_accepted" | "manual_resolution";
  state: UncertaintyResolutionRequestState;
  evidenceRef: string;
  createdAtMs: number;
  processedAtMs: number | null;
  /** Bounded code, e.g. `stale_reconcile_fence`; set when rejected or failed. */
  outcomeReason: string | null;
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
