import type { ApiResponse, PublicProofSummary } from "@/types";

// Data is refreshed weekly by the attribution job; matches the backend's own
// `Cache-Control: public, max-age=3600` on this endpoint.
const REVALIDATE_SECONDS = 3600;

/**
 * Server-side fetch for the public proof page. Deliberately bypasses
 * `lib/api-client.ts` and the Better Auth JWT-minting path — this endpoint
 * carries no auth dependency on the backend and must work for anonymous
 * visitors/crawlers even if the auth stack is unavailable.
 */
export async function getProofSummary(): Promise<PublicProofSummary> {
  const res = await fetch(
    `${process.env.API_URL}/api/v1/public/proof-summary`,
    {
      next: { revalidate: REVALIDATE_SECONDS },
    },
  );
  if (!res.ok) {
    throw new Error(`proof-summary fetch failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as ApiResponse<PublicProofSummary>;
  return body.data;
}
