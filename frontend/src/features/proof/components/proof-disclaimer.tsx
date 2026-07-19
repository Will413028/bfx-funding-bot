"use client";

import { AlertTriangle } from "lucide-react";
import { useTranslations } from "next-intl";

/**
 * Compliance-critical disclaimer block for the public proof page (2026-05-26
 * productization ADR, binding constraint 2 — 最高法院 112台上字第317號).
 * Deliberately its own component so a regression test can assert the
 * disclaimer text is actually present on the page, not just in messages/*.json.
 */
export function ProofDisclaimer() {
  const t = useTranslations("proof");

  return (
    <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-5">
      <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-500" />
      <div>
        <h3 className="font-medium text-amber-200 text-sm">
          {t("disclaimerTitle")}
        </h3>
        <p className="mt-1.5 text-xs text-zinc-400 leading-relaxed">
          {t("disclaimerBody")}
        </p>
      </div>
    </div>
  );
}
