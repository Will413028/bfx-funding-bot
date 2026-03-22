import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";

export function MarketingHeader() {
  const t = useTranslations("app");
  const tn = useTranslations("nav");
  const ta = useTranslations("auth");

  return (
    <header className="flex h-14 items-center justify-between border-b border-white/5 bg-zinc-950/80 px-6 backdrop-blur-xl">
      <div className="flex items-center gap-6">
        <Link href="/" className="text-sm font-bold tracking-tight">
          {t("name")}
        </Link>
        <nav className="hidden items-center gap-4 sm:flex">
          <Link
            href="/pricing"
            className="text-sm text-zinc-400 transition-colors hover:text-foreground"
          >
            {tn("pricing")}
          </Link>
        </nav>
      </div>
      <Link
        href="/login"
        className="rounded-md bg-white/10 px-3 py-1.5 text-sm font-medium transition-colors hover:bg-white/15 active:scale-[0.98]"
      >
        {ta("login")}
      </Link>
    </header>
  );
}
