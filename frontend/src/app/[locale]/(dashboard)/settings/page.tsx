"use client";

import { SettingsSkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { ChangePasswordForm } from "@/features/settings/components/change-password-form";
import { ProfileCard } from "@/features/settings/components/profile-card";
import { useUser } from "@/features/settings/hooks/use-user";

export default function SettingsPage() {
  const { data: user, isLoading, isError, refetch } = useUser();

  if (isLoading) {
    return <SettingsSkeleton />;
  }

  if (isError || !user) {
    return <QueryError message="Failed to load user data" onRetry={refetch} />;
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">Settings</h1>
      <ProfileCard user={user} />
      <ChangePasswordForm />
    </div>
  );
}
