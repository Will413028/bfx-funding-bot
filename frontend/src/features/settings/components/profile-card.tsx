"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import type { Profile } from "@/types";

interface ProfileCardProps {
  user: Profile;
}

export function ProfileCard({ user }: ProfileCardProps) {
  const t = useTranslations("settings");

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("account")}
      </h3>
      <dl className="mt-4 space-y-3">
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("login")}</dt>
          <dd className="text-sm text-foreground">{user.email ?? "—"}</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("userId")}</dt>
          <dd className="font-mono text-sm text-zinc-500">{user.userId}</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-sm text-zinc-400">{t("role")}</dt>
          <dd>
            <Badge variant="secondary">{user.role}</Badge>
          </dd>
        </div>
      </dl>
      <p className="mt-4 text-sm text-muted-foreground">{t("identity")}</p>
    </div>
  );
}
