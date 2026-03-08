import { getTranslations } from "next-intl/server";
import { RegisterForm } from "@/features/auth/components/register-form";
import { Link } from "@/i18n/navigation";

export default async function RegisterPage() {
  const t = await getTranslations("auth");

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">{t("register")}</h1>
        <p className="text-sm text-muted-foreground">BFX Funding Bot</p>
      </div>

      <RegisterForm />

      <p className="text-center text-sm text-muted-foreground">
        {t("hasAccount")}{" "}
        <Link
          href="/login"
          className="text-primary underline-offset-4 hover:underline"
        >
          {t("login")}
        </Link>
      </p>
    </div>
  );
}
