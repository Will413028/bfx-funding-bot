"use client";

import { useTranslations } from "next-intl";
import { StrategySkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { useSelectedExchangeAccountId } from "@/features/accounts/hooks/use-exchange-accounts";
import { StrategyForm } from "@/features/strategy/components/strategy-form";
import { useConfig } from "@/features/strategy/hooks/use-config";

export default function StrategyPage() {
  const t = useTranslations("strategy");
  const account = useSelectedExchangeAccountId();
  const {
    data: userConfig,
    isLoading,
    isError,
    refetch,
  } = useConfig(account.exchangeAccountId);

  if (account.isLoading || isLoading) {
    return <StrategySkeleton />;
  }

  if (account.isError || isError || !account.exchangeAccountId) {
    return <QueryError message={t("loadFailed")} onRetry={refetch} />;
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">{t("title")}</h1>
      <StrategyForm
        exchangeAccountId={account.exchangeAccountId}
        userConfig={userConfig ?? null}
      />
    </div>
  );
}
