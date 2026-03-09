"use client";

import { KeyRound, Plus } from "lucide-react";
import { useState } from "react";
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
  const [createOpen, setCreateOpen] = useState(false);
  const { data: apiKeys, isLoading } = useApiKeys();
  const createMutation = useCreateApiKey();
  const deleteMutation = useDeleteApiKey();
  const verifyMutation = useVerifyApiKey();

  function handleCreate(data: CreateApiKeyInput) {
    createMutation.mutate(data, {
      onSuccess: () => setCreateOpen(false),
    });
  }

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-400" />
      </div>
    );
  }

  const keys = apiKeys ?? [];
  const hasKey = keys.length > 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="font-semibold text-xl tracking-tight">API Keys</h1>
        {!hasKey && (
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            className="active:scale-[0.98]"
          >
            <Plus className="mr-1.5 size-4" />
            Add Key
          </Button>
        )}
      </div>

      {!hasKey ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-white/10 bg-white/[0.01] py-16">
          <KeyRound className="size-10 text-zinc-600" />
          <p className="mt-4 text-sm text-zinc-500">
            No API keys yet. Add your Bitfinex API key to get started.
          </p>
          <Button
            size="sm"
            className="mt-4 active:scale-[0.98]"
            onClick={() => setCreateOpen(true)}
          >
            <Plus className="mr-1.5 size-4" />
            Add Key
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
              isVerifying={verifyMutation.isPending && verifyMutation.variables === key.id}
              isDeleting={deleteMutation.isPending && deleteMutation.variables === key.id}
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
