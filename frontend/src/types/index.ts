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

// ── Dashboard ──

export interface WalletSummary {
  currency: string;
  balance: number;
  balanceAvailable: number;
}

export interface OfferSummary {
  id: number;
  currency: string;
  amount: number;
  rate: number;
  period: number;
  status: string;
  createdAt: string;
}

export interface CreditSummary {
  id: number;
  currency: string;
  amount: number;
  rate: number;
  period: number;
  status: string;
  autoRenew: boolean;
  openedAt: string;
}

export interface MarketSummary {
  frr: number;
  regime: string;
  mdcScore: number;
  flashFreeze: boolean;
  timestamp: string;
}

export interface DashboardSummary {
  wallet: WalletSummary | null;
  offers: OfferSummary[];
  credits: CreditSummary[];
  market: MarketSummary | null;
  engineReady: boolean;
}

// ── Earnings ──

export interface EarningsSummary {
  estimatedDailyEarning: number;
  weightedAPY: number;
  earnings7d: number;
  earnings30d: number;
  totalLent: number;
  activeCredits: number;
  currency: string;
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

// ── Charts ──

export interface DailyEarning {
  date: string;
  amount: number;
}

// ── WebSocket ──

export interface MarketSnapshot {
  frr: number;
  regime: string;
  mdcScore: number;
  flashFreeze: boolean;
  timestamp: string;
}
