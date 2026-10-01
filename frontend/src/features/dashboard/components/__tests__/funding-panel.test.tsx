import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";
import { FundingPanel } from "../funding-panel";

const account = "11111111-1111-4111-8111-111111111111";
const status = {
  account_id: account,
  deployment_environment: "prod",
  halt: { halted: true, reason: "operator_hold", sources: {} },
  configured_cells: [
    {
      symbol: "fUST",
      cell: "fUST_a30",
      strategy: "mean_reversion",
      period: "a30",
    },
  ],
  symbols: {
    fUST: {
      capital_available: true,
      policy_revision: 7,
      policy_digest: "verified",
      basis_token: "80",
      policy: {
        enabled: true,
        reserve_amount: "0.000000001",
        allocation_mode: "all_available",
        max_cell_fraction: "0.70",
      },
      available_balance: "9007199254740993.123456789",
      unreflected_commitments: "20.000000001",
      total_capital: "9007199254741000",
      spendable: "9007199254740973.123456787",
      unattributed_credit_exposure: "0",
      cells: {
        fUST_a30: {
          spendable: "9007199254740973.123456787",
          cell_limit: "700",
          cell_headroom: "650",
          max_new_offer: "650",
          reason: null,
        },
      },
    },
    fUSD: { capital_available: false, reason: "policy_disabled" },
  },
  dry_run: {
    account_id: account,
    deployment_environment: "prod",
    symbols: {
      fUST: {
        blocked_by: "manual_kill",
        cells: { fUST_a30: { would_submit: false, blocked_by: "manual_kill" } },
      },
    },
  },
};
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mount(fetcher: typeof fetch) {
  vi.stubGlobal("fetch", fetcher);
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      <FundingPanel exchangeAccountId={account} />
    </QueryClientProvider>,
  );
}

it("shows backend authority without rounding money or summing cell budgets", async () => {
  mount(vi.fn().mockResolvedValue(Response.json({ data: status })));
  expect(await screen.findByText("9007199254740993.123456789")).toBeTruthy();
  expect(screen.getByText("9007199254740973.123456787")).toBeTruthy();
  expect(screen.getByText("0.000000001")).toBeTruthy();
  expect(screen.getByText("policy_disabled")).toBeTruthy();
  expect(screen.getAllByText("manual_kill").length).toBeGreaterThan(0);
  expect(screen.getByText(/Applied revision: 7/)).toBeTruthy();
});

it("does not show stale amounts when status is unavailable", async () => {
  mount(
    vi
      .fn()
      .mockResolvedValue(
        Response.json(
          { detail: "funding_status_unavailable" },
          { status: 503 },
        ),
      ),
  );
  await screen.findByRole("alert");
  expect(screen.queryByText("9007199254740993.123456789")).toBeNull();
});

it("shows a missing applied policy instead of amounts", async () => {
  mount(
    vi.fn().mockResolvedValue(
      Response.json({
        data: {
          ...status,
          symbols: {
            fUST: { capital_available: false, reason: "policy_unavailable" },
            fUSD: {
              capital_available: false,
              reason: "policy_disabled",
              policy_revision: 3,
            },
          },
        },
      }),
    ),
  );
  await screen.findByText("policy_unavailable");
  expect(screen.getByText(/Applied revision: 3/)).toBeTruthy();
  expect(screen.queryByText("9007199254740993.123456789")).toBeNull();
});
