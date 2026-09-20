"use client";

import { Loader2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { QRCodeSVG } from "qrcode.react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { logoutForEnrollmentRecovery } from "@/app/[locale]/(auth)/actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { authClient } from "@/lib/auth-client";

type EnrollmentStep = "password" | "verify" | "recovery" | "complete";

interface EnrollmentSecrets {
  totpURI: string;
  backupCodes: string[];
}

export function TwoFactorEnrollment({ enrolled }: { enrolled: boolean }) {
  const t = useTranslations("securityEnrollment");
  const locale = useLocale();
  const router = useRouter();
  const [step, setStep] = useState<EnrollmentStep>(
    enrolled ? "complete" : "password",
  );
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [secrets, setSecrets] = useState<EnrollmentSecrets | null>(null);
  const [savedCodes, setSavedCodes] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const operation = useRef(0);

  useEffect(
    () => () => {
      operation.current += 1;
    },
    [],
  );

  function cancel() {
    operation.current += 1;
    setStep("password");
    setPassword("");
    setCode("");
    setSecrets(null);
    setSavedCodes(false);
    setError(null);
    setIsSubmitting(false);
  }

  async function enable(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const currentOperation = ++operation.current;
    const submittedPassword = password;
    setPassword("");
    setError(null);
    setIsSubmitting(true);

    try {
      const result = await authClient.twoFactor.enable({
        password: submittedPassword,
      });
      if (currentOperation !== operation.current) return;
      if (
        result.error ||
        !result.data?.totpURI ||
        !Array.isArray(result.data.backupCodes)
      ) {
        setError(t("startFailed"));
        return;
      }

      setSecrets({
        totpURI: result.data.totpURI,
        backupCodes: result.data.backupCodes,
      });
      setStep("verify");
    } catch {
      if (currentOperation === operation.current) {
        setError(t("startFailed"));
      }
    } finally {
      if (currentOperation === operation.current) {
        setIsSubmitting(false);
      }
    }
  }

  function acknowledgeBackupCodes() {
    setSecrets(null);
    setSavedCodes(false);
    setError(null);
  }

  function requireFreshSignIn() {
    setCode("");
    setSecrets(null);
    setSavedCodes(false);
    setError(null);
    setStep("recovery");
  }

  async function verify(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submittedCode = code.trim();
    setCode("");
    setError(null);
    if (!/^\d{6}$/.test(submittedCode)) {
      setError(t("sixDigitCodeRequired"));
      return;
    }

    const currentOperation = ++operation.current;
    setIsSubmitting(true);
    try {
      let result: Awaited<ReturnType<typeof authClient.twoFactor.verifyTotp>>;
      try {
        result = await authClient.twoFactor.verifyTotp({
          code: submittedCode,
          trustDevice: false,
        });
      } catch {
        if (currentOperation === operation.current) {
          requireFreshSignIn();
        }
        return;
      }
      if (currentOperation !== operation.current) return;
      if (result.error) {
        setError(t("invalidCode"));
        return;
      }

      let refreshed: Awaited<ReturnType<typeof authClient.getSession>>;
      try {
        refreshed = await authClient.getSession({
          query: { disableCookieCache: true },
        });
      } catch {
        if (currentOperation === operation.current) {
          requireFreshSignIn();
        }
        return;
      }
      if (currentOperation !== operation.current) return;
      if (refreshed.error || refreshed.data?.user.twoFactorEnabled !== true) {
        requireFreshSignIn();
        return;
      }

      setSecrets(null);
      setStep("complete");
    } finally {
      if (currentOperation === operation.current) {
        setIsSubmitting(false);
      }
    }
  }

  async function restartSignIn() {
    const currentOperation = ++operation.current;
    setError(null);
    setIsSubmitting(true);
    try {
      const result = await logoutForEnrollmentRecovery(locale);
      if (currentOperation !== operation.current) return;
      if (!result.success) {
        setError(t("recoveryFailed"));
        setIsSubmitting(false);
        return;
      }
      router.replace(result.redirectTo);
      router.refresh();
    } catch {
      if (currentOperation === operation.current) {
        setError(t("recoveryFailed"));
        setIsSubmitting(false);
      }
    }
  }

  if (step === "complete") {
    return (
      <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6">
        <h1 className="font-semibold text-xl">{t("title")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">{t("enabled")}</p>
      </section>
    );
  }

  if (step === "password") {
    return (
      <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6">
        <h1 className="font-semibold text-xl">{t("title")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">{t("description")}</p>
        <form onSubmit={enable} className="mt-6 space-y-4">
          <div className="space-y-2">
            <Label htmlFor="enrollment-password">{t("currentPassword")}</Label>
            <Input
              id="enrollment-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <div className="flex gap-3">
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting && <Loader2 className="animate-spin" />}
              {t("start")}
            </Button>
            <Button type="button" variant="outline" onClick={cancel}>
              {t("cancel")}
            </Button>
          </div>
        </form>
      </section>
    );
  }

  if (step === "recovery") {
    return (
      <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6">
        <h1 className="font-semibold text-xl">{t("verifyTitle")}</h1>
        <p role="alert" className="mt-2 text-sm text-destructive">
          {t("confirmationFailed")}
        </p>
        {error && (
          <p role="alert" className="mt-2 text-sm text-destructive">
            {error}
          </p>
        )}
        <Button
          type="button"
          className="mt-5"
          onClick={restartSignIn}
          disabled={isSubmitting}
        >
          {isSubmitting && <Loader2 className="animate-spin" />}
          {t("restartSignIn")}
        </Button>
      </section>
    );
  }

  if (secrets) {
    return (
      <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6">
        <h1 className="font-semibold text-xl">{t("scanTitle")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {t("scanDescription")}
        </p>
        <div className="mt-6 w-fit rounded-lg bg-white p-3">
          <QRCodeSVG
            value={secrets.totpURI}
            size={200}
            level="M"
            title={t("qrTitle")}
          />
        </div>
        <h2 className="mt-6 font-medium">{t("backupCodesTitle")}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {t("backupCodesDescription")}
        </p>
        <ul className="mt-3 grid gap-2 font-mono text-sm sm:grid-cols-2">
          {secrets.backupCodes.map((backupCode) => (
            <li key={backupCode} className="rounded bg-black/20 px-3 py-2">
              {backupCode}
            </li>
          ))}
        </ul>
        <label className="mt-5 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={savedCodes}
            onChange={(event) => setSavedCodes(event.target.checked)}
          />
          {t("savedCodes")}
        </label>
        <div className="mt-5 flex gap-3">
          <Button
            type="button"
            disabled={!savedCodes}
            onClick={acknowledgeBackupCodes}
          >
            {t("continue")}
          </Button>
          <Button type="button" variant="outline" onClick={cancel}>
            {t("cancel")}
          </Button>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-white/10 bg-white/[0.02] p-6">
      <h1 className="font-semibold text-xl">{t("verifyTitle")}</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        {t("verifyDescription")}
      </p>
      <form onSubmit={verify} className="mt-6 space-y-4">
        <div className="space-y-2">
          <Label htmlFor="enrollment-code">{t("authenticationCode")}</Label>
          <Input
            id="enrollment-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            maxLength={6}
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
        <div className="flex gap-3">
          <Button type="submit" disabled={isSubmitting}>
            {isSubmitting && <Loader2 className="animate-spin" />}
            {t("verify")}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={cancel}
            disabled={isSubmitting}
          >
            {t("cancel")}
          </Button>
        </div>
      </form>
    </section>
  );
}
