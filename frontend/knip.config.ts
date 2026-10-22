import type { KnipConfig } from "knip";

const config: KnipConfig = {
  entry: ["src/app/**/*.{ts,tsx}", "instrumentation-client.ts"],
  project: ["src/**/*.{ts,tsx}"],
  ignore: ["src/components/ui/**", "src/types/api.gen.ts"],
  ignoreDependencies: ["@tailwindcss/postcss", "babel-plugin-react-compiler"],
};

export default config;
