"use client";

import { KeyRound, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { ApiKeysSkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { Button } from "@/components/ui/button";
import { ApiKeyCard } from "@/features/api-keys/components/api-key-card";
import { CreateApiKeyDialog } from "@/features/api-keys/components/create-apikey-dialog";
import {
  useApiKeys,
  useCreateApiKey,
  useDeleteApiKey,
  useVerifyApiKey,
} from "@/features/api-keys/hooks/use-api-keys";
import type { CreateApiKeyInput } from "@/lib/validations";

export default function ApiKeysPage() {
  const t = useTranslations("apiKeys");
  const [createOpen, setCreateOpen] = useState(false);
  const { data: apiKeys, isLoading, isError, refetch } = useApiKeys();
  const createMutation = useCreateApiKey();
  const deleteMutation = useDeleteApiKey();
  const verifyMutation = useVerifyApiKey();

  function handleCreate(data: CreateApiKeyInput) {
    createMutation.mutate(data, {
      onSuccess: () => setCreateOpen(false),
    });
  }

  if (isLoading) {
    return <ApiKeysSkeleton />;
  }

  if (isError) {
    return <QueryError message={t("loadFailed")} onRetry={refetch} />;
  }

  const keys = apiKeys ?? [];
  const hasKey = keys.length > 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="font-semibold text-xl tracking-tight">{t("title")}</h1>
        {!hasKey && (
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            className="active:scale-[0.98]"
          >
            <Plus className="mr-1.5 size-4" />
            {t("addKeyShort")}
          </Button>
        )}
      </div>

      {!hasKey ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-white/10 bg-white/[0.01] py-16">
          <KeyRound className="size-10 text-zinc-600" />
          <p className="mt-4 text-sm text-zinc-500">{t("empty")}</p>
          <Button
            size="sm"
            className="mt-4 active:scale-[0.98]"
            onClick={() => setCreateOpen(true)}
          >
            <Plus className="mr-1.5 size-4" />
            {t("addKeyShort")}
          </Button>
        </div>
      ) : (
        <div className="space-y-4">
          {keys.map((key) => (
            <ApiKeyCard
              key={key.id}
              apiKey={key}
              onVerify={(id) => verifyMutation.mutate(id)}
              onDelete={(id) => deleteMutation.mutate(id)}
              isVerifying={
                verifyMutation.isPending && verifyMutation.variables === key.id
              }
              isDeleting={
                deleteMutation.isPending && deleteMutation.variables === key.id
              }
            />
          ))}
        </div>
      )}

      <CreateApiKeyDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onSubmit={handleCreate}
        isPending={createMutation.isPending}
      />
    </div>
  );
}
