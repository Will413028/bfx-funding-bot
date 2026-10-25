"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ApiKey, ApiKeyStatus } from "@/types";

interface ApiKeyCardProps {
  apiKey: ApiKey;
}

// A credential is written verified and active; rotation retires the one it replaces.
const statusClassName = {
  active: "text-emerald-400 border-emerald-400/30",
  retired: "text-zinc-500 border-zinc-500/30",
} satisfies Record<ApiKeyStatus, string>;

/** One credential, read-only: writes go through the operator CLI only. */
export function ApiKeyCard({ apiKey }: ApiKeyCardProps) {
  const t = useTranslations("apiKeys");
  const maskedKey = `${apiKey.apiKey.slice(0, 8)}...`;

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <div className="flex items-start justify-between">
        <div className="space-y-1">
          <h3 className="font-medium text-foreground">{apiKey.label}</h3>
          <p className="font-mono text-sm text-zinc-500">{maskedKey}</p>
        </div>
        <Badge variant="outline" className={cn(statusClassName[apiKey.status])}>
          {t(apiKey.status)}
        </Badge>
      </div>
    </div>
  );
}
