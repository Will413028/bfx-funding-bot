"use client";

import { Loader2, ShieldCheck, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatUSD } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ApiKey } from "@/types";
import { DeleteConfirmDialog } from "./delete-confirm-dialog";

interface ApiKeyCardProps {
  apiKey: ApiKey;
  onVerify: (id: string) => void;
  onDelete: (id: string) => void;
  isVerifying: boolean;
  isDeleting: boolean;
}

const statusConfig = {
  verified: {
    className: "text-emerald-400 border-emerald-400/30",
  },
  unverified: {
    className: "text-amber-500 border-amber-500/30",
  },
  failed: { className: "text-rose-500 border-rose-500/30" },
} as const;

const statusLabelKeys = {
  verified: "verified",
  unverified: "unverified",
  failed: "verifyFailed",
} as const;

export function ApiKeyCard({
  apiKey,
  onVerify,
  onDelete,
  isVerifying,
  isDeleting,
}: ApiKeyCardProps) {
  const t = useTranslations("apiKeys");
  const tc = useTranslations("common");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const statusKey =
    (apiKey.exchangeStatus as keyof typeof statusConfig) ?? "unverified";
  const status = statusConfig[statusKey] ?? statusConfig.unverified;
  const maskedKey = `${apiKey.apiKey.slice(0, 8)}...`;

  return (
    <>
      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <h3 className="font-medium text-foreground">{apiKey.label}</h3>
            <p className="font-mono text-sm text-zinc-500">{maskedKey}</p>
          </div>
          <Badge variant="outline" className={cn(status.className)}>
            {t(statusLabelKeys[statusKey] ?? "unverified")}
          </Badge>
        </div>

        {apiKey.exchangeStatus === "verified" && apiKey.fundingBalance && (
          <div className="mt-3 rounded-lg bg-white/[0.02] px-3 py-2">
            <p className="text-xs text-zinc-400">{t("fundingBalance")}</p>
            <p className="font-medium tabular-nums text-foreground">
              {formatUSD(apiKey.fundingBalance.balance)}{" "}
              <span className="text-xs text-zinc-500">
                {apiKey.fundingBalance.currency}
              </span>
            </p>
            <p className="text-xs text-zinc-500">
              {t("available")}: {formatUSD(apiKey.fundingBalance.available)}
            </p>
          </div>
        )}

        <div className="mt-4 flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onVerify(apiKey.id)}
            disabled={isVerifying}
            className="active:scale-[0.98]"
          >
            {isVerifying ? (
              <Loader2 className="mr-1.5 size-4 animate-spin" />
            ) : (
              <ShieldCheck className="mr-1.5 size-4" />
            )}
            {t("verify")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setDeleteOpen(true)}
            disabled={isDeleting}
            className="text-rose-500 hover:text-rose-400 active:scale-[0.98]"
          >
            {isDeleting ? (
              <Loader2 className="mr-1.5 size-4 animate-spin" />
            ) : (
              <Trash2 className="mr-1.5 size-4" />
            )}
            {tc("delete")}
          </Button>
        </div>
      </div>

      <DeleteConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        label={apiKey.label}
        onConfirm={() => onDelete(apiKey.id)}
        isDeleting={isDeleting}
      />
    </>
  );
}
