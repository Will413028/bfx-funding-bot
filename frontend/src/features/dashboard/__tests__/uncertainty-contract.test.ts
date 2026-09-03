import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@/lib/test-utils";
import type { Uncertainty } from "@/types";
import { UncertaintyBanner } from "../components/uncertainty-banner";

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
};

describe("uncertainty contract", () => {
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
      screen.getByRole("region", { name: /uncertainty status is unavailable/i }),
    ).toBeDefined();
  });
});
