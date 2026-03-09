import { Check } from "lucide-react";
import { getTranslations } from "next-intl/server";
import { Link } from "@/i18n/navigation";

const plans: {
  nameKey: string;
  price: number;
  recommended?: boolean;
  features: string[];
}[] = [
  {
    nameKey: "free",
    price: 0,
    features: ["featureDashboard", "featureManualLending"],
  },
  {
    nameKey: "starter",
    price: 9.99,
    features: [
      "featureDashboard",
      "featureAutoLending",
      "featureFixedStrategy",
      "featureEmailNotify",
    ],
  },
  {
    nameKey: "pro",
    price: 29.99,
    recommended: true,
    features: [
      "featureDashboard",
      "featureAutoLending",
      "featureAdvancedStrategy",
      "featureMarketAnalysis",
      "featurePriorityQuota",
    ],
  },
  {
    nameKey: "enterprise",
    price: 99.99,
    features: [
      "featureAllFeatures",
      "featureDedicatedSupport",
      "featureCustomParams",
    ],
  },
];

export default async function PricingPage() {
  const t = await getTranslations("pricing");

  return (
    <div className="mx-auto max-w-5xl px-6 py-20">
      <div className="text-center">
        <h1 className="font-bold text-4xl tracking-tight sm:text-5xl">
          {t("title")}
        </h1>
        <p className="mt-4 text-lg text-zinc-400">{t("subtitle")}</p>
      </div>

      <div className="mt-16 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
        {plans.map((plan) => (
          <div
            key={plan.nameKey}
            className={`relative flex flex-col rounded-xl border p-6 ${
              plan.recommended
                ? "border-white/20 bg-white/[0.04] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)]"
                : "border-white/5 bg-white/[0.02] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]"
            }`}
          >
            {plan.recommended && (
              <span className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-white px-3 py-0.5 font-medium text-xs text-zinc-950">
                {t("recommended")}
              </span>
            )}

            <h3 className="font-semibold text-lg text-foreground">
              {t(plan.nameKey)}
            </h3>

            <div className="mt-4 flex items-baseline gap-1">
              <span className="font-bold text-3xl tracking-tight">
                ${plan.price}
              </span>
              {plan.price > 0 && (
                <span className="text-sm text-zinc-500">{t("perMonth")}</span>
              )}
            </div>

            <ul className="mt-6 flex-1 space-y-3">
              {plan.features.map((featureKey) => (
                <li
                  key={featureKey}
                  className="flex items-start gap-2 text-sm text-zinc-400"
                >
                  <Check className="mt-0.5 size-4 shrink-0 text-emerald-500" />
                  {t(featureKey)}
                </li>
              ))}
            </ul>

            <Link
              href="/register"
              className={`mt-8 rounded-lg px-4 py-2.5 text-center font-medium text-sm transition-colors active:scale-[0.98] ${
                plan.recommended
                  ? "bg-white text-zinc-950 hover:bg-zinc-200"
                  : "bg-white/10 text-foreground hover:bg-white/15"
              }`}
            >
              {t("cta")}
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
