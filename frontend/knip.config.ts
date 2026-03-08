import type { KnipConfig } from "knip";

const config: KnipConfig = {
  entry: ["src/app/**/*.{ts,tsx}", "src/middleware.ts"],
  project: ["src/**/*.{ts,tsx}"],
  ignore: ["src/components/ui/**"],
  ignoreDependencies: ["@tailwindcss/postcss", "babel-plugin-react-compiler"],
};

export default config;
