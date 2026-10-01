import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@/lib/test-utils";
import type {
  Uncertainty,
  UncertaintyResolutionContext,
  UncertaintyResolutionRequest,
} from "@/types";
import { UncertaintyBanner } from "../components/uncertainty-banner";

const { resolve, mutation } = vi.hoisted(() => ({
  resolve: vi.fn(),
  mutation: { data: undefined as unknown, isPending: false },
}));

vi.mock("../hooks/use-uncertainties", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("../hooks/use-uncertainties")>();
  return {
    ...original,
    useResolveUncertainty: () => ({
      mutate: resolve,
      data: mutation.data,
      isPending: mutation.isPending,
      isError: false,
      error: null,
    }),
  };
});

vi.mock("@/features/settings/hooks/use-user", () => ({
  useUser: () => ({ data: { id: "operator-1" } }),
}));

const RESOLUTION_CONTEXT: UncertaintyResolutionContext = {
  evidenceRef: "12",
  queryStartedAtMs: 1_790_000_001_000,
  queryFinishedAtMs: 1_790_000_002_000,
  candidateCount: 1,
  candidateVenueOfferIds: ["venue-12"],
  unavailableReason: null,
};

const BLOCKED: Uncertainty = {
  uncertaintyId: "u-1",
  kind: "submit_outcome_unknown",
  symbol: "fUST",
  intendedAmount: "100",
  state: "open",
  openedEventSeq: 7,
  reconcileEventSeq: null,
  resolvedEventSeq: null,
  evidenceSummary: {
    outcomeReason: "connection_reset",
    observedAtMs: 1_790_000_000_000,
  },
  blockedScope: {
    exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
    environment: "ci",
    symbol: "fUST",
  },
  resolutionContext: RESOLUTION_CONTEXT,
};

function request(
  state: UncertaintyResolutionRequest["state"],
  outcomeReason: string | null = null,
): UncertaintyResolutionRequest {
  return {
    requestId: "req-1",
    uncertaintyId: "u-1",
    action: "bind_to_venue",
    state,
    evidenceRef: "12",
    createdAtMs: 1,
    processedAtMs: state === "requested" ? null : 2,
    resolvedEventSeq: state === "applied" ? 13 : null,
    outcomeReason,
  };
}

function bindButton() {
  return screen.getByRole("button", { name: /bind to venue offer/i });
}

describe("uncertainty contract", () => {
  beforeEach(() => {
    resolve.mockReset();
    mutation.data = undefined;
    mutation.isPending = false;
  });
  afterEach(cleanup);

  it("holds every action while the row's request waits for the daemon", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          { ...BLOCKED, resolutionRequest: request("requested") },
        ],
      }),
    );

    expect(screen.getByText(/waiting for the account daemon/i)).toBeDefined();
    expect(bindButton().hasAttribute("disabled")).toBe(true);
    fireEvent.click(bindButton());
    expect(resolve).not.toHaveBeenCalled();
  });

  it("says why the daemon did not apply a request and allows a fresh one", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          {
            ...BLOCKED,
            resolutionRequest: request("rejected", "stale_reconcile_fence"),
          },
        ],
      }),
    );

    expect(screen.getByRole("alert").textContent).toMatch(
      /not applied: stale reconcile fence/i,
    );
    expect(screen.queryByText(/waiting for the account daemon/i)).toBeNull();
    expect(bindButton().hasAttribute("disabled")).toBe(false);
  });

  it("follows the row, not an older result this tab's mutation returned", () => {
    // Another tab queued a newer request after this tab's was applied.
    mutation.data = { ...request("applied"), requestId: "req-old" };
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          {
            ...BLOCKED,
            resolutionRequest: {
              ...request("requested"),
              requestId: "req-new",
            },
          },
        ],
      }),
    );

    expect(screen.getByText(/waiting for the account daemon/i)).toBeDefined();
    expect(bindButton().hasAttribute("disabled")).toBe(true);
  });

  it("never waits on a status it has no record of", () => {
    // No second read that can fail: without a queued request on the row and
    // no submission in flight, the controls are live.
    render(createElement(UncertaintyBanner, { uncertainties: [BLOCKED] }));

    expect(screen.queryByText(/waiting for the account daemon/i)).toBeNull();
    expect(bindButton().hasAttribute("disabled")).toBe(false);
  });

  it("holds the controls while its own submission is in flight", () => {
    mutation.isPending = true;
    render(createElement(UncertaintyBanner, { uncertainties: [BLOCKED] }));

    expect(bindButton().hasAttribute("disabled")).toBe(true);
  });

  it("does not render a warning when visible symbols have no open uncertainty", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [],
        visibleSymbols: ["fUST", "fUSD"],
      }),
    );

    expect(screen.queryByRole("region")).toBeNull();
  });

  it("renders one blocked symbol while leaving unrelated symbols visible", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [BLOCKED],
        visibleSymbols: ["fUST", "fUSD"],
      }),
    );

    expect(screen.getByText("fUST")).toBeDefined();
    expect(screen.getByText(/100/)).toBeDefined();
    expect(screen.getByText("fUSD")).toBeDefined();
    expect(
      screen.queryByRole("button", { name: /retry|resubmit/i }),
    ).toBeNull();
  });

  it("renders an unavailable warning even when no uncertainty rows are available", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [],
        visibleSymbols: ["fUST"],
        isUnavailable: true,
      }),
    );

    expect(
      screen.getByRole("region", {
        name: /uncertainty status is unavailable/i,
      }),
    ).toBeDefined();
  });

  it("enables submission and echoes a ledger evidence ref byte-for-byte", () => {
    const evidenceRef = "ledger:v1:obs:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          {
            ...BLOCKED,
            resolutionContext: { ...RESOLUTION_CONTEXT, evidenceRef },
          },
        ],
      }),
    );
    expect((bindButton() as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(bindButton());
    expect(resolve).toHaveBeenCalledWith({
      uncertaintyId: "u-1",
      action: "bind-to-venue",
      evidenceRef,
      venueOfferId: "venue-12",
      operatorUuid: "operator-1",
    });
  });

  it("binds only the exact server-derived venue candidate", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [BLOCKED],
        visibleSymbols: ["fUST", "fUSD"],
      }),
    );

    expect(screen.getByText("venue-12")).toBeDefined();
    fireEvent.click(
      screen.getByRole("button", { name: /bind to venue offer/i }),
    );

    expect(resolve).toHaveBeenCalledWith({
      uncertaintyId: "u-1",
      action: "bind-to-venue",
      evidenceRef: "12",
      venueOfferId: "venue-12",
      operatorUuid: "operator-1",
    });
  });

  it("requires an explicit click before marking a proven zero-match not accepted", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          {
            ...BLOCKED,
            resolutionContext: {
              ...RESOLUTION_CONTEXT,
              candidateCount: 0,
              candidateVenueOfferIds: [],
            },
          },
        ],
      }),
    );

    expect(resolve).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: /confirm not accepted/i }),
    );
    expect(resolve).toHaveBeenCalledWith({
      uncertaintyId: "u-1",
      action: "mark-not-accepted",
      evidenceRef: "12",
      operatorUuid: "operator-1",
    });
  });

  it("does not expose an action for ambiguous server-derived candidates", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          {
            ...BLOCKED,
            resolutionContext: {
              ...RESOLUTION_CONTEXT,
              candidateCount: 2,
              candidateVenueOfferIds: ["venue-12", "venue-13"],
              unavailableReason: "multiple_exact_candidates",
            },
          },
        ],
      }),
    );

    expect(screen.getByText(/multiple exact candidates/i)).toBeDefined();
    expect(
      screen.queryByRole("button", { name: /bind|not accepted/i }),
    ).toBeNull();
  });

  it("records a bounded manual decision and required operator reason", () => {
    const orphan: Uncertainty = {
      ...BLOCKED,
      uncertaintyId: "u-orphan",
      kind: "unattributed_venue_offer",
      resolutionContext: {
        ...RESOLUTION_CONTEXT,
        candidateCount: null,
        candidateVenueOfferIds: [],
      },
    };
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [orphan],
      }),
    );

    const submit = screen.getByRole("button", {
      name: /record manual resolution/i,
    });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText(/decision/i), {
      target: { value: "closed_at_venue" },
    });
    fireEvent.change(screen.getByLabelText(/operator reason/i), {
      target: { value: "Verified directly in Bitfinex history" },
    });
    fireEvent.click(submit);

    expect(resolve).toHaveBeenCalledWith({
      uncertaintyId: "u-orphan",
      action: "manual-resolution",
      evidenceRef: "12",
      operatorUuid: "operator-1",
      reason: "Verified directly in Bitfinex history",
      evidence: { decision: "closed_at_venue" },
    });
  });
});
