import * as Sentry from "@sentry/nextjs";
import { sentryOptions } from "./src/lib/sentry";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
	Sentry.init(sentryOptions(dsn));
}
