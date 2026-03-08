import { getTranslations } from "next-intl/server";

export default async function HomePage() {
  const t = await getTranslations("common");

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-4xl font-bold">BFX Funding Bot</h1>
      <p className="text-muted-foreground">Bitfinex 自動放貸 SaaS 平台</p>
    </div>
  );
}
