// API payload types are generated from the web API's OpenAPI document
// (`api.gen.ts`; regenerate with `pnpm gen:api`, see frontend/AGENTS.md). The
// names below are the console's names for those schemas; only types the API
// does not describe are written by hand.
import type { components } from "./api.gen";

type Schemas = components["schemas"];

// ── API Response Wrappers (the api-client's view of every response) ──

export interface ApiResponse<T> {
  data: T;
}

export interface ApiErrorResponse {
  error: {
    code: string;
    message: string;
  };
}

// ── Profile ──

/** GET /api/v1/profile: the operator identity Tailscale Serve forwards. */
export type Profile = Schemas["ProfileResponse"];

// ── API Key ──

/** Account credential lifecycle as returned by the account-scoped API. */
export type ApiKey = Schemas["AccountCredentialResponse"];
export type ApiKeyStatus = ApiKey["status"];

export type ExchangeAccount = Schemas["ExchangeAccountResponse"];

// ── Strategy Config ──

export type AmountConfig = Schemas["Range"];
export type RateConfig = Schemas["Range"];
export type PeriodConfig = Schemas["Range"];
export type StrategyConfig = Schemas["StrategyConfigBody"];
export type UserConfig = Schemas["AccountConfigDraftResponse"];

// ── Funding status (the daemon's status, forwarded by the web API) ──

// Canonical live authority: Decimal values stay strings throughout the UI.
export type CapitalPolicy = Schemas["CapitalPolicyStatus"];
export type CapitalBudget = Schemas["CapitalBudgetStatus"];
export type CapitalStatus =
  | Schemas["CapitalUnavailable"]
  | Schemas["CapitalAvailable"];
export type FundingStatus = Schemas["FundingStatus"];

// ── Trading control (lending envelope ADR 2026-09-25 D4: state, resume, kill) ──

export type TradingStateView = Schemas["TradingStateView"];
export type TradingStateName = TradingStateView["state"];
export type TradingCause = TradingStateView["cause"];

export type TradingControlRequest = Schemas["TradingControlRequest"];
export type TradingControlAction = TradingControlRequest["action"];
export type TradingControlRequestState = TradingControlRequest["state"];

/** The latest venue cancel-all phase per currency for the HALTED in force. */
export type CancelAllPhase = Schemas["CancelAllPhase"];
export type BuildIdentity = Schemas["BuildIdentity"];
export type TradingControlOverview = Schemas["TradingControlOverview"];

export type CurrencyRequest = Schemas["CurrencyRequest"];
export type CurrencyAction = CurrencyRequest["action"];
/** The terms every new offer must stay inside; decimals stay strings. */
export type OfferEnvelope = Schemas["OfferEnvelope"];
/** Every currency with an applied CapitalPolicy (its own request outbox). */
export type CurrencyPolicy = Schemas["CurrencyPolicy"];

// ── SP4 Projections (operator console read models) ──

/** GET /positions: per-symbol capital as disjoint components. */
export type Position = Schemas["PositionResponse"];
/** GET /offers: managed offer (default: pending/unknown/claimed only). */
export type OfferClaim = Schemas["OfferClaimResponse"];
export type OfferClaimState = OfferClaim["state"];

/**
 * The event types the history filter offers. The API types `eventType` as a
 * string: the history also carries other names (`SUBMIT_OUTCOME_UNKNOWN`, the
 * frozen legacy archive's), which the table shows as they are.
 */
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

/** GET /executions: one history row, newest first. */
export type ExecutionEvent = Schemas["ExecutionEventResponse"];
export type ExecutionEventsPagination = Schemas["ExecutionsPagination"];
/** GET /executions: full response envelope. */
export type ExecutionEventsResponse = Schemas["ExecutionsPage"];

// ── Execution uncertainties (operator resolution) ──

/**
 * The bounded evidence keys the backend allowlists
 * (`operator_commands.evidence_summary`). The API types the summary as an open
 * map of scalars, so this narrowing is the console's.
 */
export interface UncertaintyEvidenceSummary {
  outcomeReason?: string | null;
  observedAtMs?: number | null;
  candidateCount?: number | null;
  venueOfferId?: string | null;
  status?: string | null;
  coverage?: Record<string, boolean>;
}

export type UncertaintyBlockedScope = Schemas["BlockedScope"];
/** Latest server-derived evidence that can authorize an operator resolution. */
export type UncertaintyResolutionContext =
  Schemas["UncertaintyResolutionContext"];

/** Account/environment/symbol-scoped execution block. */
export type Uncertainty = Omit<
  Schemas["UncertaintyResponse"],
  "evidenceSummary"
> & { evidenceSummary: UncertaintyEvidenceSummary };
export type UncertaintyKind = Uncertainty["kind"];
export type UncertaintyState = Uncertainty["state"];

/**
 * One queued operator adjudication. The web API only records the request; the
 * account daemon applies it and reports the outcome here (ADR D4').
 */
export type UncertaintyResolutionRequest = Schemas["ResolutionRequestResponse"];
export type UncertaintyResolutionRequestState =
  UncertaintyResolutionRequest["state"];

// ── Attribution ──

export type WeeklyAttributionPoint = Schemas["WeeklyAttributionResponse"];
