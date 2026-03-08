import { getTranslations } from "next-intl/server";

export default async function OverviewPage() {
  const t = await getTranslations("overview");

  return (
    <div className="p-8">
      <h1 className="text-2xl font-bold">{t("title")}</h1>
      <p className="mt-2 text-muted-foreground">Placeholder</p>
    </div>
  );
}
