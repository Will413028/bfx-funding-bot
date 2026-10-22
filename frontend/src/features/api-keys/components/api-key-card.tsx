"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ApiKey, ApiKeyStatus } from "@/types";

interface ApiKeyCardProps {
  apiKey: ApiKey;
}

const statusConfig = {
  pending: {
    className: "text-amber-500 border-amber-500/30",
  },
  verified: {
    className: "text-emerald-400 border-emerald-400/30",
  },
  unverified: {
    className: "text-amber-500 border-amber-500/30",
  },
  failed: { className: "text-rose-500 border-rose-500/30" },
  revoked: { className: "text-rose-500 border-rose-500/30" },
  retired: { className: "text-zinc-500 border-zinc-500/30" },
} as const;

const statusLabelKeys = {
  pending: "pending",
  verified: "verified",
  unverified: "unverified",
  failed: "verifyFailed",
  revoked: "revoked",
  retired: "retired",
} satisfies Record<ApiKeyStatus, string>;

/** One credential, read-only: writes go through the operator CLI only. */
export function ApiKeyCard({ apiKey }: ApiKeyCardProps) {
  const t = useTranslations("apiKeys");
  const statusKey: ApiKeyStatus =
    apiKey.status in statusConfig ? apiKey.status : "unverified";
  const status = statusConfig[statusKey] ?? statusConfig.unverified;
  const maskedKey = `${apiKey.apiKey.slice(0, 8)}...`;
  const showsVerificationError =
    ["pending", "failed", "revoked"].includes(statusKey) &&
    Boolean(apiKey.lastVerifyError);

  return (
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

      {showsVerificationError && apiKey.lastVerifyError && (
        <p
          data-testid="verify-error-reason"
          className="mt-3 rounded-lg bg-rose-500/5 px-3 py-2 text-xs text-rose-400"
        >
          {t("verifyFailedReason")}: {apiKey.lastVerifyError}
        </p>
      )}
    </div>
  );
}
