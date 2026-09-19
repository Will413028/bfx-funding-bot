import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@/lib/test-utils";
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
      snapshot_seq: 80,
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
const prepared = {
  id: "22222222-2222-4222-8222-222222222222",
  state: "prepared",
  symbol: "fUST",
  cell: "fUST_a30",
  strategy: "mean_reversion",
  max_amount: "200.000000001",
  minimum_amount: "153",
  exact_amount: null,
  expires_at_ms: Date.now() + 600000,
  request_revision: 1,
  processed_revision: 1,
  halt_id: 7,
  binding: { source_revision: "abc" },
  evidence: { preparation: { max_new_offer: "650" } },
  reason: null,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
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

it("prepares then requires a separate human authorize request with the observed revision", async () => {
  const requests: { url: string; body: unknown }[] = [];
  mount(
    vi.fn(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST")
        requests.push({ url, body: JSON.parse(String(init.body)) });
      return Response.json({
        data: url.includes("funding-status") ? status : prepared,
      });
    }),
  );
  await screen.findByText(/Applied revision: 7/);
  fireEvent.change(screen.getByLabelText("Maximum canary amount (USDT)"), {
    target: { value: "200.000000001" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Prepare session" }));
  await screen.findByText("prepared");
  expect(requests).toHaveLength(1);
  expect(requests[0].body).toMatchObject({
    symbol: "fUST",
    cell: "fUST_a30",
    strategy: "mean_reversion",
    max_amount: "200.000000001",
  });
  expect(requests[0].body).not.toHaveProperty("binding");
  fireEvent.click(
    screen.getByLabelText("I authorize this single real-money offer"),
  );
  fireEvent.click(screen.getByRole("button", { name: "Authorize canary" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1].body).toEqual({ expected_revision: 1 });
  expect(requests[1].url).toContain(`/${prepared.id}/authorize`);
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
  expect(screen.queryByRole("button", { name: "Prepare session" })).toBeNull();
});

it("blocks preparation when initial applied policy is missing", async () => {
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
  expect(
    (
      screen.getByRole("button", {
        name: "Prepare session",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

it("validation never promotes and promotion requires a fresh explicit human confirmation", async () => {
  localStorage.setItem(`release-session:${account}`, prepared.id);
  let row = {
    ...prepared,
    state: "observed",
    request_revision: 4,
    processed_revision: 4,
  };
  const requests: { url: string; body: unknown }[] = [];
  mount(
    vi.fn(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST") {
        requests.push({ url, body: JSON.parse(String(init.body)) });
        row = {
          ...row,
          state: "validated",
          request_revision: 5,
          processed_revision: 5,
        };
      }
      return Response.json({
        data: url.includes("funding-status") ? status : row,
      });
    }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "Validate outcome while halted",
    }),
  );
  const promote = await screen.findByRole("button", {
    name: "Promote release",
  });
  expect((promote as HTMLButtonElement).disabled).toBe(true);
  expect(requests).toHaveLength(1);
  expect(requests[0].url).toContain("/validate");
  expect(requests[0].body).toEqual({ expected_revision: 4 });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(promote);
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1].url).toContain("/promote");
  expect(requests[1].body).toEqual({ expected_revision: 5 });
});

it("does not carry human confirmation across a newly observed revision", async () => {
  localStorage.setItem(`release-session:${account}`, prepared.id);
  let row = prepared;
  mount(
    vi.fn(async (input) =>
      Response.json({
        data: String(input).includes("funding-status") ? status : row,
      }),
    ),
  );
  const authorize = await screen.findByRole("button", {
    name: "Authorize canary",
  });
  fireEvent.click(screen.getByRole("checkbox"));
  expect((authorize as HTMLButtonElement).disabled).toBe(false);
  row = {
    ...prepared,
    request_revision: 2,
    processed_revision: 2,
    minimum_amount: "154",
  };
  fireEvent.click(screen.getByRole("button", { name: "Refresh state" }));
  await screen.findByText(/154/);
  expect((authorize as HTMLButtonElement).disabled).toBe(true);
});

it.each(["persisted", "manual"])(
  "recovers from a %s missing session using only reads and local deselection",
  async (selection) => {
    if (selection === "persisted")
      localStorage.setItem(`release-session:${account}`, prepared.id);
    const requests: { url: string; method: string }[] = [];
    mount(
      vi.fn(async (input, init) => {
        const url = String(input);
        requests.push({ url, method: init?.method ?? "GET" });
        return url.includes("funding-status")
          ? Response.json({ data: status })
          : Response.json(
              { detail: "release_session_not_found" },
              { status: 404 },
            );
      }),
    );
    await screen.findByText(/Applied revision: 7/);
    if (selection === "manual") {
      fireEvent.change(screen.getByLabelText("Existing session ID"), {
        target: { value: prepared.id },
      });
      fireEvent.click(screen.getByRole("button", { name: "Open session" }));
    }
    await screen.findByRole("alert");
    expect(
      screen.queryByRole("button", { name: "Prepare session" }),
    ).toBeNull();
    const readsBefore = requests.filter((r) =>
      r.url.endsWith(prepared.id),
    ).length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh state" }));
    await waitFor(() =>
      expect(
        requests.filter((r) => r.url.endsWith(prepared.id)).length,
      ).toBeGreaterThan(readsBefore),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Leave session selection" }),
    );
    expect(localStorage.getItem(`release-session:${account}`)).toBeNull();
    expect(
      await screen.findByRole("button", { name: "Prepare session" }),
    ).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(requests.every((r) => r.method === "GET")).toBe(true);
  },
);
