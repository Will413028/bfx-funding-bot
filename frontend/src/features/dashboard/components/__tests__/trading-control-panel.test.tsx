import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@/lib/test-utils";
import type {
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
    probation: null,
    ...extra,
  };
}

function overview(
  extra: Partial<TradingControlOverview> = {},
): TradingControlOverview {
  return {
    trading_state: stateOf("ACTIVE", "operator"),
    cancel_all: [],
    running: {
      backend_digest: DIGEST,
      source_revision: REVISION,
      change_class: "standard",
    },
    latest_deployment: null,
    approvals: [],
    requests: [],
    ...extra,
  };
}

function requestRow(
  extra: Partial<TradingControlRequest>,
): TradingControlRequest {
  return {
    request_id: "22222222-2222-4222-8222-222222222222",
    action: "resume",
    backend_digest: DIGEST,
    reason: "done",
    requested_by: "will",
    created_at_ms: 1,
    state: "requested",
    processed_at_ms: null,
    outcome_reason: null,
    trading_state_id: null,
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
      { data: { request_id: "x", action: "pause", state: "requested" } },
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

it("shows the probation limit, floor and the lift's progress", async () => {
  mount(
    overview({
      trading_state: stateOf("ACTIVE", "operator", {
        probation: {
          multiplier: "0.25",
          started_at_ms: 1,
          floor: { fUST: "150.75" },
          elapsed_ms: 11 * 3_600_000 + 5,
          required_ms: 24 * 3_600_000,
          acknowledged: 2,
          required_acknowledged: 3,
        },
      }),
    }),
  );
  expect(await screen.findByText(/limited to 25% of the normal/)).toBeTruthy();
  expect(screen.getByText("Minimum offer fUST: 150.75")).toBeTruthy();
  expect(screen.getByText("11h of 24h")).toBeTruthy();
  expect(screen.getByText("2 of 3 acknowledged submits")).toBeTruthy();
});

it("a material deploy awaiting approval names the build and approves exactly it", async () => {
  const sent = mount(
    overview({
      trading_state: stateOf("REDUCING", "material_deploy"),
      running: {
        backend_digest: DIGEST,
        source_revision: REVISION,
        change_class: "material",
      },
    }),
  );
  expect(
    await screen.findByText("Material deploy awaiting approval"),
  ).toBeTruthy();
  expect(screen.getByText(DIGEST)).toBeTruthy();
  expect(screen.getByText(REVISION)).toBeTruthy();
  expect(screen.getByText("material")).toBeTruthy();
  // Approval first: there is no resume around it.
  expect(screen.queryByRole("button", { name: "Resume trading" })).toBeNull();
  const approve = screen.getByRole("button", {
    name: "Approve this build",
  }) as HTMLButtonElement;
  expect(approve.disabled).toBe(true); // a reason is required
  fireEvent.change(
    screen.getByLabelText("Reason (recorded with your request)"),
    {
      target: { value: "reviewed the diff" },
    },
  );
  fireEvent.click(approve);
  await waitFor(() =>
    expect(sent.filter((s) => s.method === "POST")).toHaveLength(1),
  );
  expect(sent.find((s) => s.method === "POST")).toEqual({
    url: `${base}/approve`,
    method: "POST",
    body: { reason: "reviewed the diff", backend_digest: DIGEST },
  });
});

it("pause and resume go through the web API, a stop naming no build", async () => {
  const sent = mount(overview());
  fireEvent.change(
    await screen.findByLabelText("Reason (recorded with your request)"),
    { target: { value: "maintenance" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Pause (cancels only)" }));
  await waitFor(() =>
    expect(sent.filter((s) => s.method === "POST")).toHaveLength(1),
  );
  expect(sent.find((s) => s.method === "POST")).toEqual({
    url: `${base}/pause`,
    method: "POST",
    body: { reason: "maintenance" },
  });
  // Never the bot's static-token admin API.
  expect(sent.every((s) => s.url.startsWith(base))).toBe(true);
});

it("resume is offered after a stop, with the probation rule stated", async () => {
  const sent = mount(
    overview({
      trading_state: stateOf("HALTED", "auto"),
      cancel_all: [
        { currency: "UST", phase: "acknowledged", detail: null, at_ms: 1 },
      ],
    }),
  );
  expect(
    await screen.findByText(/resumes inside a 24-hour probation/),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Pause (cancels only)" }),
  ).toBeNull();
  fireEvent.change(
    screen.getByLabelText("Reason (recorded with your request)"),
    { target: { value: "loss explained" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Resume trading" }));
  await waitFor(() =>
    expect(sent.filter((s) => s.method === "POST")).toHaveLength(1),
  );
  expect(sent.find((s) => s.method === "POST")?.body).toEqual({
    reason: "loss explained",
    backend_digest: DIGEST,
  });
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
      trading_state: stateOf("REDUCING", "operator"),
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
    (screen.getByRole("button", { name: "Kill switch…" }) as HTMLButtonElement)
      .disabled,
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
          outcome_reason: "approval_required",
        }),
      ],
    }),
  );
  expect(
    await screen.findByText("Refused: resume — approval required"),
  ).toBeTruthy();
});

it("shows the web API's refusal of a request", async () => {
  mount(overview(), () =>
    Response.json({ detail: "request_pending" }, { status: 409 }),
  );
  fireEvent.change(
    await screen.findByLabelText("Reason (recorded with your request)"),
    { target: { value: "maintenance" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Pause (cancels only)" }));
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

it("offers approval only for a material build that is not approved yet", async () => {
  const material = {
    backend_digest: DIGEST,
    source_revision: REVISION,
    change_class: "material",
  };
  mount(
    overview({
      trading_state: stateOf("HALTED", "operator"),
      running: material,
      approvals: [
        {
          backend_digest: DIGEST,
          source_revision: REVISION,
          approved_by: "will",
          approved_at_ms: 1,
        },
      ],
    }),
  );
  expect(
    await screen.findByRole("button", { name: "Resume trading" }),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Approve this build" }),
  ).toBeNull();
  cleanup();
  mount(
    overview({
      trading_state: stateOf("HALTED", "operator"),
      running: material,
    }),
  );
  expect(
    await screen.findByRole("button", { name: "Approve this build" }),
  ).toBeTruthy();
});
