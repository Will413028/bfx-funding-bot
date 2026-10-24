import { describe, expect, it } from "vitest";
import { strategyConfigSchema } from "../validations";

describe("strategyConfigSchema", () => {
  const validConfig = {
    currency: "USD",
    amount: { min: 50, max: 1000 },
    rate: { min: 0.0001, max: 0.001 },
    period: { min: 2, max: 30 },
    autoRenew: true,
  };

  it("accepts valid config", () => {
    const result = strategyConfigSchema.safeParse(validConfig);
    expect(result.success).toBe(true);
  });

  it("rejects when min > max (amount)", () => {
    const result = strategyConfigSchema.safeParse({
      ...validConfig,
      amount: { min: 1000, max: 50 },
    });
    expect(result.success).toBe(false);
  });

  it("rejects when min > max (rate)", () => {
    const result = strategyConfigSchema.safeParse({
      ...validConfig,
      rate: { min: 0.01, max: 0.001 },
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty currency", () => {
    const result = strategyConfigSchema.safeParse({
      ...validConfig,
      currency: "",
    });
    expect(result.success).toBe(false);
  });

  it.each(["US D", "<script>", "f".repeat(17)])(
    "rejects a currency the backend refuses (%s)",
    (currency) => {
      expect(
        strategyConfigSchema.safeParse({ ...validConfig, currency }).success,
      ).toBe(false);
    },
  );

  it("rejects a non-finite bound", () => {
    const result = strategyConfigSchema.safeParse({
      ...validConfig,
      amount: { min: 1, max: Number.POSITIVE_INFINITY },
    });
    expect(result.success).toBe(false);
  });
});
