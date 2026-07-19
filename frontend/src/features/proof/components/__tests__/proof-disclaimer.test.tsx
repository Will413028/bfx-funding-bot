import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import { ProofDisclaimer } from "../proof-disclaimer";

// Compliance regression guard (2026-05-26 productization ADR, constraint 2):
// the public proof page must always render a prominent past-performance
// disclaimer, and that disclaimer must never drift into promissory language.
// NOTE: bare "guarantee" is legitimately present ("not a guarantee", "does
// not guarantee") — a compliant disclaimer negates guarantees, it doesn't
// avoid the word. Only check for the promise-making phrasings themselves.
const FORBIDDEN_PHRASES = [
  "expected return",
  "guaranteed return",
  "guaranteed profit",
  "sure profit",
  "risk-free",
  "risk free",
];

describe("ProofDisclaimer", () => {
  it("renders the disclaimer title and body", () => {
    const { container } = render(<ProofDisclaimer />);
    const text = container.textContent ?? "";
    expect(text).toContain(
      "Past performance is not indicative of future results",
    );
    expect(text).toContain("historical data only");
  });

  it("explicitly negates guaranteed/promised returns (not merely silent on them)", () => {
    const { container } = render(<ProofDisclaimer />);
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).toContain(
      "not a projection, prediction, promise, or guarantee",
    );
    expect(text).toContain("investment advice");
  });

  it("never uses promissory/guaranteed-return language", () => {
    const { container } = render(<ProofDisclaimer />);
    const text = (container.textContent ?? "").toLowerCase();
    for (const phrase of FORBIDDEN_PHRASES) {
      expect(text).not.toContain(phrase);
    }
  });
});
