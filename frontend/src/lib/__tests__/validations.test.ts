import { describe, expect, it } from "vitest";
import {
  changePasswordSchema,
  createApiKeySchema,
  loginSchema,
  strategyConfigSchema,
} from "../validations";

describe("loginSchema", () => {
  it("accepts valid input", () => {
    const result = loginSchema.safeParse({
      email: "a@b.com",
      password: "12345678",
    });
    expect(result.success).toBe(true);
  });

  it("rejects invalid email", () => {
    const result = loginSchema.safeParse({
      email: "invalid",
      password: "12345678",
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty password", () => {
    const result = loginSchema.safeParse({
      email: "a@b.com",
      password: "",
    });
    expect(result.success).toBe(false);
  });
});

describe("createApiKeySchema", () => {
  it("accepts valid input", () => {
    const result = createApiKeySchema.safeParse({
      label: "my key",
      apiKey: "key123",
      apiSecret: "secret123",
    });
    expect(result.success).toBe(true);
  });

  it("rejects empty label", () => {
    const result = createApiKeySchema.safeParse({
      label: "",
      apiKey: "key123",
      apiSecret: "secret123",
    });
    expect(result.success).toBe(false);
  });

  it("rejects label over 50 chars", () => {
    const result = createApiKeySchema.safeParse({
      label: "a".repeat(51),
      apiKey: "key123",
      apiSecret: "secret123",
    });
    expect(result.success).toBe(false);
  });
});

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
});

describe("changePasswordSchema", () => {
  it("accepts matching passwords", () => {
    const result = changePasswordSchema.safeParse({
      currentPassword: "oldpass123",
      newPassword: "newpass123",
      confirmPassword: "newpass123",
    });
    expect(result.success).toBe(true);
  });

  it("rejects mismatched passwords", () => {
    const result = changePasswordSchema.safeParse({
      currentPassword: "oldpass123",
      newPassword: "newpass123",
      confirmPassword: "different",
    });
    expect(result.success).toBe(false);
  });

  it("rejects short new password", () => {
    const result = changePasswordSchema.safeParse({
      currentPassword: "oldpass123",
      newPassword: "short",
      confirmPassword: "short",
    });
    expect(result.success).toBe(false);
  });
});
