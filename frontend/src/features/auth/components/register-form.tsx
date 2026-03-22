"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { register as registerAction } from "@/app/[locale]/(auth)/actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRouter } from "@/i18n/navigation";
import { type RegisterFormInput, registerSchema } from "@/lib/validations";

export function RegisterForm() {
  const t = useTranslations("auth");
  const tv = useTranslations("validation");
  const router = useRouter();
  const [serverError, setServerError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterFormInput>({
    resolver: zodResolver(registerSchema),
  });

  async function onSubmit(data: RegisterFormInput) {
    setServerError(null);

    let result: Awaited<ReturnType<typeof registerAction>>;
    try {
      result = await registerAction(data.email, data.password);
    } catch {
      setServerError(t("registrationFailed"));
      return;
    }

    if (!result.success) {
      setServerError(result.error ?? t("registrationFailed"));
      return;
    }

    router.push("/overview");
    router.refresh();
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="email">{t("email")}</Label>
        <Input
          id="email"
          type="email"
          autoComplete="email"
          aria-invalid={!!errors.email}
          aria-describedby={errors.email ? "reg-email-error" : undefined}
          {...register("email")}
        />
        {errors.email && (
          <p
            id="reg-email-error"
            role="alert"
            className="text-sm text-destructive"
          >
            {tv(errors.email.message as string)}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="password">{t("password")}</Label>
        <Input
          id="password"
          type="password"
          autoComplete="new-password"
          aria-invalid={!!errors.password}
          aria-describedby={errors.password ? "reg-password-error" : undefined}
          {...register("password")}
        />
        {errors.password && (
          <p
            id="reg-password-error"
            role="alert"
            className="text-sm text-destructive"
          >
            {tv(errors.password.message as string)}
          </p>
        )}
      </div>

      {serverError && (
        <p role="alert" className="text-sm text-destructive">
          {serverError}
        </p>
      )}

      <Button
        type="submit"
        disabled={isSubmitting}
        className="w-full active:scale-[0.98]"
      >
        {isSubmitting && <Loader2 className="animate-spin" />}
        {t("register")}
      </Button>
    </form>
  );
}
