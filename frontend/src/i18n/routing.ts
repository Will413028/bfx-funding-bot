import { defineRouting } from "next-intl/routing";

export const routing = defineRouting({
  locales: ["en", "zh-TW"],
  defaultLocale: "en",
  // The locale is the URL's; a static export has no server to read a cookie, so none is
  // written (ASVS V3.3.1/V3.3.3: no cookie, nothing to secure).
  localeCookie: false,
});
