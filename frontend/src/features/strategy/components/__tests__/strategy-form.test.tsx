import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";
import type { UserConfig } from "@/types";
import { StrategyForm } from "../strategy-form";

vi.mock("../../hooks/use-config", () => ({
  useSaveConfig: () => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false }),
  useResetConfig: () => ({ mutate: vi.fn(), isPending: false }),
}));

function draft(overrides: Partial<UserConfig> = {}): UserConfig {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
    config: {
      currency: "fUST",
      amount: { min: 150, max: 5000 },
      rate: { min: 0.0001, max: 0.002 },
      period: { min: 2, max: 30 },
      autoRenew: true,
    },
    configError: null,
    revision: 3,
    source: "operator",
    createdAt: "2026-10-01T08:30:00+00:00",
    updatedAt: "2026-10-02T09:45:00+00:00",
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("StrategyForm", () => {
  it("fills the form from a readable stored draft", () => {
    render(<StrategyForm exchangeAccountId="acc" userConfig={draft()} />);

    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe(
      "fUST",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names an unreadable stored draft and starts from the defaults so it can be overwritten", () => {
    render(
      <StrategyForm
        exchangeAccountId="acc"
        userConfig={draft({ config: null, configError: "config_unreadable" })}
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("revision 3");
    expect((screen.getByLabelText("Currency") as HTMLInputElement).value).toBe(
      "USD",
    );
    expect(
      (screen.getByRole("button", { name: /save/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});
