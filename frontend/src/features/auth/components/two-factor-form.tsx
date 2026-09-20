"use client";

import { Loader2 } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRouter } from "@/i18n/navigation";
import { authClient } from "@/lib/auth-client";
import { safeCallbackUrl } from "@/lib/safe-callback-url";

export function TwoFactorForm() {
  const t = useTranslations("auth");
  const router = useRouter();
  const searchParams = useSearchParams();
  const [code, setCode] = useState("");
  const [method, setMethod] = useState<"totp" | "backup">("totp");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      const result =
        method === "totp"
          ? await authClient.twoFactor.verifyTotp({
              code: code.trim(),
              trustDevice: false,
            })
          : await authClient.twoFactor.verifyBackupCode({
              code: code.trim(),
              trustDevice: false,
            });
      if (result.error) {
        setError(
          t(method === "totp" ? "invalidTwoFactorCode" : "invalidBackupCode"),
        );
        return;
      }
      router.push(safeCallbackUrl(searchParams.get("callbackUrl")));
      router.refresh();
    } catch {
      setError(
        t(method === "totp" ? "invalidTwoFactorCode" : "invalidBackupCode"),
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      <div>
        <h1 className="font-semibold text-lg">{t("twoFactorTitle")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t(
            method === "totp"
              ? "twoFactorDescription"
              : "backupCodeDescription",
          )}
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="two-factor-code">
          {t(method === "totp" ? "authenticationCode" : "backupCode")}
        </Label>
        <Input
          id="two-factor-code"
          inputMode={method === "totp" ? "numeric" : "text"}
          autoComplete={method === "totp" ? "one-time-code" : "off"}
          pattern={method === "totp" ? "[0-9]{6,8}" : undefined}
          maxLength={method === "totp" ? 8 : 128}
          value={code}
          onChange={(event) => setCode(event.target.value)}
          required
        />
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <Button type="submit" disabled={isSubmitting} className="w-full">
        {isSubmitting && <Loader2 className="animate-spin" />}
        {t("verify")}
      </Button>
      <Button
        type="button"
        variant="link"
        disabled={isSubmitting}
        onClick={() => {
          setMethod(method === "totp" ? "backup" : "totp");
          setCode("");
          setError(null);
        }}
      >
        {t(method === "totp" ? "useBackupCode" : "useAuthenticationCode")}
      </Button>
    </form>
  );
}
