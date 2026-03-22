"use client";

import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col items-center justify-center gap-4 bg-[hsl(220,20%,6%)] text-[hsl(210,20%,92%)]">
        <h1 className="text-2xl font-bold">Something went wrong</h1>
        <p className="text-sm opacity-60">
          {process.env.NODE_ENV === "development"
            ? error.message
            : "An unexpected error occurred"}
        </p>
        <button
          type="button"
          onClick={reset}
          className="rounded bg-[hsl(217,91%,60%)] px-4 py-2 text-white"
        >
          Try again
        </button>
      </body>
    </html>
  );
}
