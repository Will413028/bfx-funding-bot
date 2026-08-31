import { getTranslations } from "next-intl/server";
import { LoginForm } from "@/features/auth/components/login-form";

export default async function LoginPage() {
  const t = await getTranslations("auth");

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">{t("login")}</h1>
        <p className="text-sm text-muted-foreground">BFX Funding Bot</p>
      </div>

      <LoginForm />
    </div>
  );
}
