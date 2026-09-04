import { Activity, BarChart3, Bot, ShieldCheck } from "lucide-react";
import { getTranslations } from "next-intl/server";
import { Link } from "@/i18n/navigation";

const features = [
  { icon: Bot, titleKey: "feature1Title", descKey: "feature1Desc" },
  { icon: BarChart3, titleKey: "feature2Title", descKey: "feature2Desc" },
  { icon: ShieldCheck, titleKey: "feature3Title", descKey: "feature3Desc" },
  { icon: Activity, titleKey: "feature4Title", descKey: "feature4Desc" },
] as const;

export default async function LandingPage() {
  const t = await getTranslations("landing");

  return (
    <div>
      {/* Hero */}
      <section className="flex flex-col items-center px-6 py-24 text-center">
        <h1 className="max-w-3xl font-bold text-4xl tracking-tight sm:text-5xl">
          {t("heroTitle")}
        </h1>
        <p className="mt-4 max-w-xl text-lg text-zinc-400">
          {t("heroSubtitle")}
        </p>
        <Link
          href="/login"
          className="mt-8 rounded-lg bg-white px-6 py-2.5 font-medium text-sm text-zinc-950 transition-colors hover:bg-zinc-200 active:scale-[0.98]"
        >
          {t("getStarted")}
        </Link>
      </section>

      {/* Features */}
      <section className="mx-auto max-w-5xl px-6 py-16">
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {features.map((f) => (
            <div
              key={f.titleKey}
              className="rounded-xl border border-white/5 bg-white/[0.02] p-6 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]"
            >
              <f.icon className="size-8 text-zinc-400" />
              <h3 className="mt-4 font-semibold text-foreground">
                {t(f.titleKey)}
              </h3>
              <p className="mt-2 text-sm text-zinc-500">{t(f.descKey)}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Bottom CTA */}
      <section className="flex flex-col items-center px-6 py-20 text-center">
        <h2 className="font-bold text-2xl tracking-tight sm:text-3xl">
          {t("ctaTitle")}
        </h2>
        <p className="mt-3 text-zinc-400">{t("ctaSubtitle")}</p>
        <Link
          href="/login"
          className="mt-6 rounded-lg bg-white px-6 py-2.5 font-medium text-sm text-zinc-950 transition-colors hover:bg-zinc-200 active:scale-[0.98]"
        >
          {t("ctaButton")}
        </Link>
      </section>
    </div>
  );
}
