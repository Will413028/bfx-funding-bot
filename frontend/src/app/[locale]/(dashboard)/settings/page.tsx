"use client";

import { useTranslations } from "next-intl";
import { SettingsSkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { ChangePasswordForm } from "@/features/settings/components/change-password-form";
import { ProfileCard } from "@/features/settings/components/profile-card";
import { useUser } from "@/features/settings/hooks/use-user";
import { Link } from "@/i18n/navigation";

export default function SettingsPage() {
  const t = useTranslations("settings");
  const { data: user, isLoading, isError, refetch } = useUser();

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">{t("title")}</h1>
      <Link
        href="/settings/security"
        className="block rounded-xl border border-white/10 bg-white/[0.02] p-4 text-sm font-medium transition-colors hover:bg-white/[0.05]"
      >
        {t("security")}
      </Link>
      {isLoading ? (
        <SettingsSkeleton />
      ) : isError || !user ? (
        <QueryError message={t("loadFailed")} onRetry={refetch} />
      ) : (
        <>
          <ProfileCard user={user} />
          <ChangePasswordForm />
        </>
      )}
    </div>
  );
}
