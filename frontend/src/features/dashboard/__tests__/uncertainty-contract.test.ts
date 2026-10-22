import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";
import type {
  Uncertainty,
  UncertaintyResolutionContext,
  UncertaintyResolutionRequest,
} from "@/types";
import { UncertaintyBanner } from "../components/uncertainty-banner";

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
  evidenceSummary: {
    outcomeReason: "connection_reset",
    observedAtMs: 1_790_000_000_000,
  },
  blockedScope: {
    exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
    environment: "ci",
    symbol: "fUST",
  },
  resolvedByOperatorId: null,
  resolutionReason: null,
  resolutionContext: RESOLUTION_CONTEXT,
  resolutionRequest: null,
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
    outcomeReason,
  };
}

const CLI_COMMAND =
  "sudo bfx-op bind-to-venue|mark-not-accepted|manual-resolution u-1 --evidence-ref …";

/** The detail view offers no adjudication control: verdicts go through the CLI. */
function expectNoVerdictControls() {
  expect(screen.queryAllByRole("button")).toHaveLength(0);
  expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  expect(screen.queryAllByRole("combobox")).toHaveLength(0);
}

describe("uncertainty contract", () => {
  afterEach(cleanup);

  it("shows a request queued from the CLI as waiting for the daemon", () => {
    render(
      createElement(UncertaintyBanner, {
        uncertainties: [
          { ...BLOCKED, resolutionRequest: request("requested") },
        ],
      }),
    );

    expect(screen.getByText(/waiting for the account daemon/i)).toBeDefined();
    expectNoVerdictControls();
  });

  it("says why the daemon did not apply a request", () => {
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
  });

  it("never waits on a status it has no record of", () => {
    render(createElement(UncertaintyBanner, { uncertainties: [BLOCKED] }));

    expect(screen.queryByText(/waiting for the account daemon/i)).toBeNull();
  });

  it.each([
    ["submit_outcome_unknown", 1, ["venue-12"]],
    ["submit_outcome_unknown", 0, []],
    ["unattributed_venue_offer", null, []],
  ] as const)(
    "offers no verdict control, only the CLI command (%s, %s candidates)",
    (kind, candidateCount, candidateVenueOfferIds) => {
      render(
        createElement(UncertaintyBanner, {
          uncertainties: [
            {
              ...BLOCKED,
              kind,
              resolutionContext: {
                ...RESOLUTION_CONTEXT,
                candidateCount,
                candidateVenueOfferIds: [...candidateVenueOfferIds],
              },
            },
          ],
        }),
      );

      expectNoVerdictControls();
      expect(screen.getByText(/Adjudicate in the operator CLI/)).toBeDefined();
      expect(screen.getByText(CLI_COMMAND)).toBeDefined();
    },
  );

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
    // The server-derived candidate stays visible for the CLI verdict.
    expect(screen.getByText("venue-12")).toBeDefined();
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
});
