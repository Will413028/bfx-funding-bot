"use client";

import { KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { ApiKeysSkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { useSelectedExchangeAccountId } from "@/features/accounts/hooks/use-exchange-accounts";
import { ApiKeyCard } from "@/features/api-keys/components/api-key-card";
import { useApiKeys } from "@/features/api-keys/hooks/use-api-keys";

/**
 * Read-only list of the account's Bitfinex credentials. Keys are added only by
 * the operator CLI bootstrap and replaced only by `bfx-op rotate-credential`.
 */
export default function ApiKeysPage() {
  const t = useTranslations("apiKeys");
  const account = useSelectedExchangeAccountId();
  const {
    data: apiKeys,
    isLoading,
    isError,
    refetch,
  } = useApiKeys(account.exchangeAccountId);

  if (account.isLoading || isLoading) {
    return <ApiKeysSkeleton />;
  }

  if (account.isError || isError || !account.exchangeAccountId) {
    return <QueryError message={t("loadFailed")} onRetry={refetch} />;
  }

  const keys = apiKeys ?? [];

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="font-semibold text-xl tracking-tight">{t("title")}</h1>
        <p className="text-sm text-zinc-500">{t("cliHint")}</p>
      </div>

      {keys.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-white/10 bg-white/[0.01] py-16">
          <KeyRound className="size-10 text-zinc-600" />
          <p className="mt-4 text-sm text-zinc-500">{t("empty")}</p>
        </div>
      ) : (
        <div className="space-y-4">
          {keys.map((key) => (
            <ApiKeyCard key={key.id} apiKey={key} />
          ))}
        </div>
      )}
    </div>
  );
}
