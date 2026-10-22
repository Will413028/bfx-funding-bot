import { z } from "zod";
import type { StrategyConfig } from "@/types";

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

// The form schema is UX only; the backend's StrategyConfigBody is the contract.
// Valid form values must be a valid PUT body, or tsc fails here.
type AssertAssignable<T extends U, U> = T;
type _FormValuesAreAConfigBody = AssertAssignable<
  StrategyConfigInput,
  StrategyConfig
>;
