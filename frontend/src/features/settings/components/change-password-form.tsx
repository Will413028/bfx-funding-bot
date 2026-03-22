"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { useForm } from "react-hook-form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  type ChangePasswordInput,
  changePasswordSchema,
} from "@/lib/validations";
import { useChangePassword } from "../hooks/use-user";

export function ChangePasswordForm() {
  const t = useTranslations("settings");
  const mutation = useChangePassword();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<ChangePasswordInput>({
    resolver: zodResolver(changePasswordSchema),
  });

  function onSubmit(data: ChangePasswordInput) {
    mutation.mutate(
      { currentPassword: data.currentPassword, newPassword: data.newPassword },
      {
        onSuccess: () => {
          reset();
          setTimeout(() => mutation.reset(), 2000);
        },
      },
    );
  }

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("changePassword")}
      </h3>
      <form onSubmit={handleSubmit(onSubmit)} className="mt-4 space-y-4">
        <div className="space-y-2">
          <Label htmlFor="currentPassword">{t("currentPassword")}</Label>
          <Input
            id="currentPassword"
            type="password"
            {...register("currentPassword")}
          />
          {errors.currentPassword && (
            <p className="text-xs text-rose-500">
              {errors.currentPassword.message}
            </p>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor="newPassword">{t("newPassword")}</Label>
          <Input
            id="newPassword"
            type="password"
            {...register("newPassword")}
          />
          {errors.newPassword && (
            <p className="text-xs text-rose-500">
              {errors.newPassword.message}
            </p>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor="confirmPassword">{t("confirmPassword")}</Label>
          <Input
            id="confirmPassword"
            type="password"
            {...register("confirmPassword")}
          />
          {errors.confirmPassword && (
            <p className="text-xs text-rose-500">
              {errors.confirmPassword.message}
            </p>
          )}
        </div>

        {mutation.isError && (
          <p className="text-xs text-rose-500">
            {mutation.error?.message ?? t("changePasswordFailed")}
          </p>
        )}

        <Button
          type="submit"
          disabled={mutation.isPending}
          className="active:scale-[0.98]"
        >
          {mutation.isPending ? (
            <Loader2 className="mr-1.5 size-4 animate-spin" />
          ) : (
            <Save className="mr-1.5 size-4" />
          )}
          {mutation.isSuccess ? t("passwordChanged") : t("changePassword")}
        </Button>
      </form>
    </div>
  );
}
