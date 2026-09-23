import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
	resolve: {
		alias: {
			"@": resolve(__dirname, "src"),
		},
	},
	test: {
		include: ["src/**/*.test.{ts,tsx}"],
		environment: "jsdom",
		// next-intl's ESM imports "next/server" without an extension, which
		// Node's resolver rejects; let Vite resolve it instead.
		server: { deps: { inline: ["next-intl"] } },
		env: {
			API_URL: "http://localhost:8000",
		},
	},
});
