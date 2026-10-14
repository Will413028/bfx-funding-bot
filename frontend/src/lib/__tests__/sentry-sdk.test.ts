import * as Sentry from "@sentry/nextjs";
import { afterEach, describe, expect, it } from "vitest";
import { sentryOptions } from "../sentry";

// The real SDK, not a hand-built event: what beforeSend receives carries SDK
// internals (scopes in sdkProcessingMetadata) that reference each other.
describe("browser SDK with sentryOptions", () => {
  afterEach(async () => {
    await Sentry.close(0);
  });

  it("sends a captured error, scrubbed", async () => {
    const sent: string[] = [];
    Sentry.init({
      ...sentryOptions("https://publickey@o1.ingest.us.sentry.io/1"),
      transport: () => ({
        send: async (envelope: unknown) => {
          sent.push(JSON.stringify(envelope));
          return {};
        },
        flush: async () => true,
      }),
    });

    Sentry.captureException(new Error("failed for operator@example.com"));
    await Sentry.flush(2000);

    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain("failed for [redacted]");
    expect(sent[0]).not.toContain("operator@example.com");
  });
});
