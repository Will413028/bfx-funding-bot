import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@/lib/test-utils";
import type {
  CurrencyPolicy,
  CurrencyRequest,
  TradingControlOverview,
  TradingControlRequest,
  TradingStateView,
} from "@/types";
import { TradingControlPanel } from "../trading-control-panel";

const account = "11111111-1111-4111-8111-111111111111";
const DIGEST = `sha256:${"a".repeat(64)}`;
const REVISION = "c".repeat(40);
const base = `/api/proxy/exchange-accounts/${account}/trading-control`;

function stateOf(
  state: TradingStateView["state"],
  cause: TradingStateView["cause"],
  extra: Partial<TradingStateView> = {},
): TradingStateView {
  return {
    id: 7,
    state,
    cause,
    actor: "will",
    reason: "venue incident",
    at_ms: Date.UTC(2026, 8, 25, 12),
    ...extra,
  };
}

function overview(
  extra: Partial<TradingControlOverview> = {},
): TradingControlOverview {
  return {
    trading_state: stateOf("ACTIVE", "operator"),
    cancel_all: [],
    running: { backend_digest: DIGEST, source_revision: REVISION },
    latest_deployment: null,
    requests: [],
    currencies: [],
    ...extra,
  };
}

function requestRow(
  extra: Partial<TradingControlRequest>,
): TradingControlRequest {
  return {
    request_id: "22222222-2222-4222-8222-222222222222",
    action: "resume",
    reason: "done",
    requested_by: "will",
    created_at_ms: 1,
    state: "requested",
    processed_at_ms: null,
    outcome_reason: null,
    ...extra,
  };
}

interface Sent {
  url: string;
  method: string;
  body: unknown;
}

function mount(
  data: TradingControlOverview | (() => Response),
  post: (sent: Sent) => Response = () =>
    Response.json(
      { data: { request_id: "x", action: "resume", state: "requested" } },
      { status: 202 },
    ),
) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input)).pathname;
      const method = init?.method ?? "GET";
      if (method === "POST") {
        const entry = { url, method, body: JSON.parse(String(init?.body)) };
        sent.push(entry);
        return post(entry);
      }
      sent.push({ url, method, body: null });
      return typeof data === "function" ? data() : Response.json({ data });
    }),
  );
  render(
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
      <TradingControlPanel exchangeAccountId={account} />
    </QueryClientProvider>,
  );
  return sent;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows the state, its cause, who decided it and why", async () => {
  mount(overview({ trading_state: stateOf("HALTED", "auto") }));
  expect(await screen.findByText("Halted · automatic protection")).toBeTruthy();
  expect(screen.getByText("By will: venue incident")).toBeTruthy();
  expect(screen.getByText(/^Since /)).toBeTruthy();
});

it("reads no recorded decision as halted", async () => {
  mount(overview({ trading_state: null }));
  expect(
    await screen.findByText(
      "No trading decision recorded — treated as HALTED.",
    ),
  ).toBeTruthy();
});

it("resume is offered after a stop and goes through the web API naming no build", async () => {
  const sent = mount(
    overview({
      trading_state: stateOf("HALTED", "auto"),
      cancel_all: [
        { currency: "UST", phase: "acknowledged", detail: null, at_ms: 1 },
      ],
    }),
  );
  expect(await screen.findByText(/no probation follows/)).toBeTruthy();
  fireEvent.change(
    screen.getByLabelText("Reason (recorded with your request)"),
    { target: { value: "loss explained" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Resume trading" }));
  await waitFor(() =>
    expect(sent.filter((s) => s.method === "POST")).toHaveLength(1),
  );
  expect(sent.find((s) => s.method === "POST")).toEqual({
    url: `${base}/resume`,
    method: "POST",
    body: { reason: "loss explained" },
  });
  // Never the bot's static-token admin API.
  expect(sent.every((s) => s.url.startsWith(base))).toBe(true);
});

it("an active state offers no resume, only the kill switch", async () => {
  mount(overview());
  expect(
    await screen.findByRole("button", { name: "Kill switch…" }),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Resume trading" })).toBeNull();
  expect(
    screen.getAllByRole("button").map((button) => button.textContent),
  ).toEqual(["Kill switch…"]);
});

it("the kill switch needs a reason and the typed phrase before it is sent", async () => {
  const sent = mount(overview());
  fireEvent.click(await screen.findByRole("button", { name: "Kill switch…" }));
  const confirm = screen.getByRole("button", {
    name: "Confirm kill",
  }) as HTMLButtonElement;
  const fieldset = screen.getByRole("group", { name: "Kill switch" });
  const [reason, phrase] = Array.from(
    fieldset.querySelectorAll("input"),
  ) as HTMLInputElement[];
  expect(confirm.disabled).toBe(true);
  fireEvent.change(phrase, { target: { value: "KILL" } });
  expect(confirm.disabled).toBe(true); // a reason is required too
  fireEvent.change(reason, { target: { value: "venue incident" } });
  fireEvent.change(phrase, { target: { value: "kill" } });
  expect(confirm.disabled).toBe(true); // exactly KILL
  fireEvent.change(phrase, { target: { value: "KILL" } });
  expect(confirm.disabled).toBe(false);
  fireEvent.click(confirm);
  await waitFor(() =>
    expect(sent.filter((s) => s.method === "POST")).toHaveLength(1),
  );
  expect(sent.find((s) => s.method === "POST")).toEqual({
    url: `${base}/kill`,
    method: "POST",
    body: { reason: "venue incident" },
  });
});

it("cancelling the kill confirmation sends nothing", async () => {
  const sent = mount(overview());
  fireEvent.click(await screen.findByRole("button", { name: "Kill switch…" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("button", { name: "Confirm kill" })).toBeNull();
  expect(sent.some((s) => s.method === "POST")).toBe(false);
});

it("a halt whose cancel-all did not land says offers may remain", async () => {
  mount(
    overview({
      trading_state: stateOf("HALTED", "operator"),
      cancel_all: [
        { currency: "UST", phase: "acknowledged", detail: null, at_ms: 1 },
        { currency: "USD", phase: "failed", detail: "venue down", at_ms: 1 },
      ],
    }),
  );
  expect(await screen.findByText("USD: failed — venue down")).toBeTruthy();
  expect(
    screen.getByText(/The venue may still hold funding offers/),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Retry kill switch…" }),
  ).toBeTruthy();
});

it("a halt with no cancel-all recorded says so", async () => {
  mount(overview({ trading_state: stateOf("HALTED", "auto") }));
  expect(
    await screen.findByText(/No venue cancel-all is recorded/),
  ).toBeTruthy();
});

it("while a request waits, other actions wait too but the kill does not", async () => {
  mount(
    overview({
      trading_state: stateOf("HALTED", "operator"),
      requests: [requestRow({ action: "resume" })],
    }),
  );
  expect(
    await screen.findByText("Waiting for the trading daemon: resume"),
  ).toBeTruthy();
  fireEvent.change(
    screen.getByLabelText("Reason (recorded with your request)"),
    { target: { value: "again" } },
  );
  expect(
    (
      screen.getByRole("button", {
        name: "Resume trading",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "Retry kill switch…",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
});

it("shows why the daemon refused the last request", async () => {
  mount(
    overview({
      trading_state: stateOf("HALTED", "auto"),
      requests: [
        requestRow({
          state: "rejected",
          processed_at_ms: 2,
          outcome_reason: "operator_not_authorized",
        }),
      ],
    }),
  );
  expect(
    await screen.findByText("Refused: resume — operator not authorized"),
  ).toBeTruthy();
});

it("shows the web API's refusal of a request", async () => {
  mount(overview({ trading_state: stateOf("HALTED", "operator") }), () =>
    Response.json({ detail: "request_pending" }, { status: 409 }),
  );
  fireEvent.change(
    await screen.findByLabelText("Reason (recorded with your request)"),
    { target: { value: "back" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Resume trading" }));
  expect(
    await screen.findByText("Another request is still waiting for the daemon."),
  ).toBeTruthy();
});

it("an unreadable state leaves nothing to act on but the kill switch", async () => {
  const sent = mount(() =>
    Response.json({ detail: "trading_state_unavailable" }, { status: 503 }),
  );
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(
    screen.getAllByRole("button").map((button) => button.textContent),
  ).toEqual(["Kill switch…"]);
  fireEvent.click(screen.getByRole("button", { name: "Kill switch…" }));
  const fieldset = screen.getByRole("group", { name: "Kill switch" });
  const [reason, phrase] = Array.from(
    fieldset.querySelectorAll("input"),
  ) as HTMLInputElement[];
  fireEvent.change(reason, { target: { value: "state unreadable" } });
  fireEvent.change(phrase, { target: { value: "KILL" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm kill" }));
  await waitFor(() =>
    expect(sent.find((s) => s.method === "POST")?.url).toBe(`${base}/kill`),
  );
});

// ── Currency enable/disable ─────────────────────────────────────────────────

function currency(extra: Partial<CurrencyPolicy> = {}): CurrencyPolicy {
  return {
    symbol: "fUST",
    revision: 3,
    policy_error: null,
    enabled: true,
    max_offer_amount: "200",
    envelope: {
      min_period_days: 2,
      max_period_days: 2,
      max_open_offers: 6,
      rate_floor_ratio: "0.5",
      min_rate_apr: "0.01",
    },
    requests: [],
    ...extra,
  };
}

function currencyRequest(extra: Partial<CurrencyRequest>): CurrencyRequest {
  return {
    request_id: "33333333-3333-4333-8333-333333333333",
    symbol: "fUST",
    action: "disable",
    reason: "maintenance",
    requested_by: "will",
    created_at_ms: 1,
    state: "requested",
    processed_at_ms: null,
    outcome_reason: null,
    ...extra,
  };
}

it("shows each currency's state and offer limits, or that the limits are unset", async () => {
  mount(
    overview({
      currencies: [
        currency({ symbol: "fUSD", enabled: false, envelope: null }),
        currency(),
      ],
    }),
  );
  expect(await screen.findByText("fUST · Enabled")).toBeTruthy();
  expect(
    screen.getByText(
      "Up to 200 per offer · 2–2 days · at most 6 open offers · rate floor: the higher of 1% a year and 0.5 × the median bid",
    ),
  ).toBeTruthy();
  expect(screen.getByText("fUSD · Disabled")).toBeTruthy();
  expect(screen.getByText(/Offer limits not set/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Disable fUST" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Enable fUSD" })).toBeTruthy();
});

it("disabling a currency needs a reason and only queues a request", async () => {
  const sent = mount(overview({ currencies: [currency()] }), () =>
    Response.json(
      {
        data: {
          request_id: "x",
          symbol: "fUST",
          action: "disable",
          state: "requested",
        },
      },
      { status: 202 },
    ),
  );
  const button = (await screen.findByRole("button", {
    name: "Disable fUST",
  })) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Reason for the fUST change"), {
    target: { value: "venue maintenance" },
  });
  expect(button.disabled).toBe(false);
  fireEvent.click(button);
  await waitFor(() =>
    expect(sent.filter((s) => s.method === "POST")).toHaveLength(1),
  );
  expect(sent.find((s) => s.method === "POST")).toEqual({
    url: `${base}/currencies/fUST/disable`,
    method: "POST",
    body: { reason: "venue maintenance" },
  });
});

it("a waiting toggle holds its button but never the kill switch", async () => {
  mount(
    overview({
      currencies: [currency({ requests: [currencyRequest({})] })],
    }),
  );
  expect(
    await screen.findByText("Waiting for the trading daemon: disable"),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Reason for the fUST change"), {
    target: { value: "again" },
  });
  expect(
    (screen.getByRole("button", { name: "Disable fUST" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("button", { name: "Kill switch…" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});

it("shows the daemon's outcome of the last toggle", async () => {
  mount(
    overview({
      currencies: [
        currency({
          enabled: false,
          requests: [
            currencyRequest({
              action: "enable",
              state: "rejected",
              processed_at_ms: 2,
              outcome_reason: "superseded_by_kill",
            }),
          ],
        }),
      ],
    }),
  );
  expect(
    await screen.findByText("Refused: enable — superseded by kill"),
  ).toBeTruthy();
});

it("an unreadable policy is shown and offers no toggle", async () => {
  mount(
    overview({
      currencies: [
        currency({
          policy_error: "invalid_policy_schema_or_digest",
          enabled: null,
          max_offer_amount: null,
          envelope: null,
        }),
      ],
    }),
  );
  expect(await screen.findByText("fUST · Policy unreadable")).toBeTruthy();
  expect(screen.getByText(/invalid policy schema or digest/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /fUST/ })).toBeNull();
});

it("shows the web API's refusal of a toggle", async () => {
  mount(overview({ currencies: [currency()] }), () =>
    Response.json({ detail: "policy_unavailable" }, { status: 404 }),
  );
  fireEvent.change(await screen.findByLabelText("Reason for the fUST change"), {
    target: { value: "x" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Disable fUST" }));
  expect(
    await screen.findByText("This currency has no applied policy."),
  ).toBeTruthy();
});
