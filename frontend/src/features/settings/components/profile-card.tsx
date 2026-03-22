"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { User } from "@/types";

const statusConfig = {
  active: {
    className: "text-emerald-400 border-emerald-400/30",
  },
  suspended: {
    className: "text-rose-500 border-rose-500/30",
  },
} as const;

interface ProfileCardProps {
  user: User;
}

export function ProfileCard({ user }: ProfileCardProps) {
  const t = useTranslations("settings");
  const status = statusConfig[user.status] ?? statusConfig.active;
  const statusLabel =
    user.status === "suspended" ? t("suspended") : t("active");

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("account")}
      </h3>
      <dl className="mt-4 space-y-3">
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("email")}</dt>
          <dd className="text-sm text-foreground">{user.email}</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("statusLabel")}</dt>
          <dd>
            <Badge variant="outline" className={cn(status.className)}>
              {statusLabel}
            </Badge>
          </dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("plan")}</dt>
          <dd>
            <Badge variant="secondary">{user.plan}</Badge>
          </dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("memberSince")}</dt>
          <dd className="text-sm text-zinc-500">
            {new Date(user.createdAt).toLocaleDateString()}
          </dd>
        </div>
      </dl>
    </div>
  );
}
