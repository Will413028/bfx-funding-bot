"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { fundingStatusKeys } from "@/lib/query-keys";
import type { FundingStatus } from "@/types";

/** The applied capital policy and budgets, exactly as the daemon computes them. */
export function FundingPanel({
  exchangeAccountId,
}: {
  exchangeAccountId: string;
}) {
  const t = useTranslations("funding");
  const status = useQuery({
    queryKey: fundingStatusKeys.detail(exchangeAccountId),
    queryFn: () =>
      apiClient.get<FundingStatus>(
        accountScopedPath(exchangeAccountId, "/funding-status"),
      ),
    refetchInterval: 15000,
  });

  if (status.isError) return <p role="alert">{t("unavailable")}</p>;
  if (!status.data) return <p>{t("loading")}</p>;
  const data = status.data;

  return (
    <section className="space-y-4 rounded-xl border p-5">
      <h2 className="text-lg font-semibold">{t("title")}</h2>
      <p>
        {data.halt.halted ? t("halted") : t("notHalted")} {data.halt.reason}
      </p>
      {Object.entries(data.symbols).map(([symbol, capital]) => (
        <section key={symbol} className="space-y-2 border-t pt-3">
          <h3>{symbol === "fUST" ? "USDT" : "USD"}</h3>
          {!capital.capital_available ? (
            <>
              <output>{capital.reason}</output>
              {capital.policy_revision !== undefined && (
                <p>{t("revision", { revision: capital.policy_revision })}</p>
              )}
            </>
          ) : (
            <>
              <p>
                {capital.policy.enabled ? t("enabled") : t("disabled")} ·{" "}
                {t("revision", { revision: capital.policy_revision })}
              </p>
              <dl className="grid gap-2 sm:grid-cols-2">
                {(
                  [
                    ["available", capital.available_balance],
                    ["pending", capital.unreflected_commitments],
                    ["reserve", capital.policy.reserve_amount],
                    ["spendable", capital.spendable],
                    ["total", capital.total_capital],
                    ["unattributed", capital.unattributed_credit_exposure],
                  ] as const
                ).map(([label, value]) => (
                  <div key={label}>
                    <dt>{t(label)}</dt>
                    <dd className="break-all font-mono">{value}</dd>
                  </div>
                ))}
              </dl>
              <p>{t("sharedBudget")}</p>
              {Object.entries(capital.cells).map(([cell, budget]) => (
                <div key={cell} className="text-sm">
                  <span>
                    {cell}: {t("cellHeadroom")} {budget.cell_headroom} ·{" "}
                    {t("cellBudget")} {budget.max_new_offer}
                  </span>
                  <p>
                    {budget.reason ??
                      data.dry_run.symbols[symbol]?.cells[cell]?.blocked_by}
                  </p>
                </div>
              ))}
            </>
          )}
          <p>{data.dry_run.symbols[symbol]?.blocked_by}</p>
        </section>
      ))}
    </section>
  );
}
