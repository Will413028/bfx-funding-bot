"use client";

import { ChangePasswordForm } from "@/features/settings/components/change-password-form";
import { ProfileCard } from "@/features/settings/components/profile-card";
import { useUser } from "@/features/settings/hooks/use-user";

export default function SettingsPage() {
  const { data: user, isLoading } = useUser();

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-400" />
      </div>
    );
  }

  if (!user) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-zinc-500">Failed to load user data</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">Settings</h1>
      <ProfileCard user={user} />
      <ChangePasswordForm />
    </div>
  );
}
