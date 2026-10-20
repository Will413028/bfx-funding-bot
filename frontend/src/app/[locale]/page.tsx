"use client";

import { useEffect } from "react";
import { useRouter } from "@/i18n/navigation";

// The locale root opens the overview. A static export cannot redirect on the server; the
// web API redirects `/` itself, and this covers `/<locale>/`.
export default function LocaleRootPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/overview");
  }, [router]);
  return null;
}
