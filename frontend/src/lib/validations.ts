import { z } from "zod";

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
