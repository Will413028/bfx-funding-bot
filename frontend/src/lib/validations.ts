import { z } from "zod";

export const loginSchema = z.object({
  email: z.string().min(1, "required").email("invalidEmail"),
  password: z.string().min(1, "required"),
});

export type LoginFormInput = z.infer<typeof loginSchema>;

export const createApiKeySchema = z.object({
  label: z.string().min(1, "required").max(50, "maxLength"),
  apiKey: z.string().min(1, "required"),
  apiSecret: z.string().min(1, "required"),
});

export type CreateApiKeyInput = z.infer<typeof createApiKeySchema>;

const rangeSchema = (_label: string) =>
  z
    .object({
      min: z.number({ error: "required" }).positive(),
      max: z.number({ error: "required" }).positive(),
    })
    .refine((d) => d.min <= d.max, {
      message: "rangeMinMax",
      path: ["min"],
    });

export const strategyConfigSchema = z.object({
  currency: z.string().min(1, "required"),
  amount: rangeSchema("Amount"),
  rate: rangeSchema("Rate"),
  period: rangeSchema("Period"),
  autoRenew: z.boolean(),
});

export type StrategyConfigInput = z.infer<typeof strategyConfigSchema>;

export const changePasswordSchema = z
  .object({
    currentPassword: z.string().min(1, "required"),
    newPassword: z.string().min(1, "required").min(8, "minLength"),
    confirmPassword: z.string().min(1, "required"),
  })
  .refine((d) => d.newPassword === d.confirmPassword, {
    message: "passwordMismatch",
    path: ["confirmPassword"],
  });

export type ChangePasswordInput = z.infer<typeof changePasswordSchema>;
