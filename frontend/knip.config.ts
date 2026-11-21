import type { KnipConfig } from "knip";

const config: KnipConfig = {
  entry: ["src/app/**/*.{ts,tsx}"],
  project: ["src/**/*.{ts,tsx}"],
  ignore: ["src/components/ui/**", "src/types/api.gen.ts"],
  // Imported from src/app/globals.css (`@import` / `@plugin`); knip does not
  // follow CSS imports.
  ignoreDependencies: ["tailwindcss", "tailwindcss-animate"],
};

export default config;
