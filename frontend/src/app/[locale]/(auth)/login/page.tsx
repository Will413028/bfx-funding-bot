import { getTranslations } from "next-intl/server";

export default async function LoginPage() {
  const t = await getTranslations("auth");

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">{t("login")}</h1>
      <p className="text-muted-foreground">Placeholder</p>
    </div>
  );
}
