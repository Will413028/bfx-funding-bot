import { notFound } from "next/navigation";

// Unmatched paths under a locale render the locale not-found page, which is
// dynamic and so carries the CSP nonce; the root fallback is prerendered and
// its scripts would be blocked by the policy.
export default function CatchAllPage() {
  notFound();
}
