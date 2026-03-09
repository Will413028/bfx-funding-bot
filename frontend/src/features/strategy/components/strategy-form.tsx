"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2, RotateCcw, Save } from "lucide-react";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  type StrategyConfigInput,
  strategyConfigSchema,
} from "@/lib/validations";
import type { StrategyConfig, UserConfig } from "@/types";
import { useResetConfig, useSaveConfig } from "../hooks/use-config";

const DEFAULTS: StrategyConfigInput = {
  currency: "USD",
  amount: { min: 50, max: 10000 },
  rate: { min: 3.65, max: 36.5 },
  period: { min: 2, max: 30 },
  autoRenew: true,
};

function toFormValues(config: StrategyConfig): StrategyConfigInput {
  return {
    currency: config.currency,
    amount: config.amount,
    rate: {
      min: Number.parseFloat((config.rate.min * 365 * 100).toFixed(4)),
      max: Number.parseFloat((config.rate.max * 365 * 100).toFixed(4)),
    },
    period: config.period,
    autoRenew: config.autoRenew,
  };
}

function toApiPayload(values: StrategyConfigInput): StrategyConfig {
  return {
    currency: values.currency,
    amount: values.amount,
    rate: {
      min: Number.parseFloat((values.rate.min / 365 / 100).toFixed(10)),
      max: Number.parseFloat((values.rate.max / 365 / 100).toFixed(10)),
    },
    period: values.period,
    autoRenew: values.autoRenew,
  };
}

interface StrategyFormProps {
  userConfig: UserConfig | null;
}

export function StrategyForm({ userConfig }: StrategyFormProps) {
  const saveMutation = useSaveConfig();
  const resetMutation = useResetConfig();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<StrategyConfigInput>({
    resolver: zodResolver(strategyConfigSchema),
    defaultValues: userConfig ? toFormValues(userConfig.config) : DEFAULTS,
  });

  useEffect(() => {
    if (userConfig) {
      reset(toFormValues(userConfig.config));
    }
  }, [userConfig, reset]);

  function onSave(data: StrategyConfigInput) {
    saveMutation.mutate(toApiPayload(data), {
      onSuccess: () => {
        setTimeout(() => saveMutation.reset(), 2000);
      },
    });
  }

  function onReset() {
    if (
      !window.confirm(
        "Reset strategy to defaults? This will delete your saved config.",
      )
    ) {
      return;
    }
    resetMutation.mutate(undefined, {
      onSuccess: () => reset(DEFAULTS),
    });
  }

  return (
    <form onSubmit={handleSubmit(onSave)} className="space-y-6">
      {/* Basic Settings */}
      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
        <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          Basic Settings
        </h3>
        <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="currency">Currency</Label>
            <Input id="currency" disabled {...register("currency")} />
          </div>
          <div className="flex items-end gap-3 pb-0.5">
            <label
              htmlFor="autoRenew"
              className="flex items-center gap-2 text-sm"
            >
              <input
                id="autoRenew"
                type="checkbox"
                className="size-4 rounded border-white/10 bg-white/5"
                {...register("autoRenew")}
              />
              Auto Renew
            </label>
          </div>
        </div>
      </div>

      {/* Range Parameters */}
      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
        <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          Amount Range (USD)
        </h3>
        <div className="mt-4 grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor="amount-min">Min</Label>
            <Input
              id="amount-min"
              type="number"
              step="1"
              className="tabular-nums"
              {...register("amount.min", { valueAsNumber: true })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="amount-max">Max</Label>
            <Input
              id="amount-max"
              type="number"
              step="1"
              className="tabular-nums"
              {...register("amount.max", { valueAsNumber: true })}
            />
          </div>
        </div>
        {(errors.amount?.min || errors.amount?.max) && (
          <p className="mt-1 text-xs text-rose-500">
            {errors.amount?.min?.message || errors.amount?.max?.message}
          </p>
        )}
      </div>

      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
        <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          Rate Range (APR %)
        </h3>
        <div className="mt-4 grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor="rate-min">Min</Label>
            <Input
              id="rate-min"
              type="number"
              step="0.01"
              className="tabular-nums"
              {...register("rate.min", { valueAsNumber: true })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="rate-max">Max</Label>
            <Input
              id="rate-max"
              type="number"
              step="0.01"
              className="tabular-nums"
              {...register("rate.max", { valueAsNumber: true })}
            />
          </div>
        </div>
        {(errors.rate?.min || errors.rate?.max) && (
          <p className="mt-1 text-xs text-rose-500">
            {errors.rate?.min?.message || errors.rate?.max?.message}
          </p>
        )}
      </div>

      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
        <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          Period Range (Days)
        </h3>
        <div className="mt-4 grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor="period-min">Min</Label>
            <Input
              id="period-min"
              type="number"
              step="1"
              className="tabular-nums"
              {...register("period.min", { valueAsNumber: true })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="period-max">Max</Label>
            <Input
              id="period-max"
              type="number"
              step="1"
              className="tabular-nums"
              {...register("period.max", { valueAsNumber: true })}
            />
          </div>
        </div>
        {(errors.period?.min || errors.period?.max) && (
          <p className="mt-1 text-xs text-rose-500">
            {errors.period?.min?.message || errors.period?.max?.message}
          </p>
        )}
      </div>

      {/* Actions */}
      <div className="flex gap-3">
        <Button
          type="submit"
          disabled={saveMutation.isPending}
          className="active:scale-[0.98]"
        >
          {saveMutation.isPending ? (
            <Loader2 className="mr-1.5 size-4 animate-spin" />
          ) : (
            <Save className="mr-1.5 size-4" />
          )}
          {saveMutation.isSuccess ? "Saved!" : "Save"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={resetMutation.isPending}
          onClick={onReset}
          className="active:scale-[0.98]"
        >
          {resetMutation.isPending ? (
            <Loader2 className="mr-1.5 size-4 animate-spin" />
          ) : (
            <RotateCcw className="mr-1.5 size-4" />
          )}
          Reset to Defaults
        </Button>
      </div>
      {(saveMutation.isError || resetMutation.isError) && (
        <p className="text-sm text-rose-500">
          {saveMutation.error?.message ?? resetMutation.error?.message ?? "Operation failed"}
        </p>
      )}
    </form>
  );
}
