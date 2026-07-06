"use client";

import {
  BarChart3,
  Key,
  LayoutDashboard,
  LineChart,
  LogOut,
  ScrollText,
  Settings,
} from "lucide-react";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { logout } from "@/app/[locale]/(auth)/actions";
import { Link } from "@/i18n/navigation";
import { routing } from "@/i18n/routing";
import { cn } from "@/lib/utils";

const localePrefix = new RegExp(`^/(${routing.locales.join("|")})`);

const navItems = [
  { href: "/overview", icon: LayoutDashboard, labelKey: "overview" },
  { href: "/api-keys", icon: Key, labelKey: "apiKeys" },
  { href: "/strategy", icon: BarChart3, labelKey: "strategy" },
  { href: "/history", icon: ScrollText, labelKey: "history" },
  { href: "/attribution", icon: LineChart, labelKey: "attribution" },
  { href: "/settings", icon: Settings, labelKey: "settings" },
] as const;

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const t = useTranslations("nav");
  const locale = useLocale();
  const pathname = usePathname();

  // Strip locale prefix for matching (e.g. /en/overview → /overview)
  const pathnameWithoutLocale = pathname.replace(localePrefix, "") || "/";

  return (
    <nav
      aria-label="Main navigation"
      className="flex flex-1 flex-col justify-between"
    >
      <ul className="flex flex-col gap-1 px-3 py-2">
        {navItems.map((item) => {
          const isActive = pathnameWithoutLocale.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                onClick={onNavigate}
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-white/[0.08] text-foreground"
                    : "text-muted-foreground hover:bg-white/[0.04] hover:text-foreground",
                )}
              >
                <item.icon className="size-4" />
                {t(item.labelKey)}
              </Link>
            </li>
          );
        })}
      </ul>

      <div className="border-t border-white/5 px-3 py-3">
        <button
          type="button"
          onClick={() => logout(locale)}
          className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-white/[0.04] hover:text-foreground"
        >
          <LogOut className="size-4" />
          {t("logout")}
        </button>
      </div>
    </nav>
  );
}
