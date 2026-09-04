import { toNextJsHandler } from "better-auth/next-js";
import { NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const authHandler = toNextJsHandler(auth);
const BLOCKED_JWT_ENDPOINTS = new Set([
  "/api/auth/token",
  "/api/auth/sign-jwt",
  "/api/auth/verify-jwt",
]);

function isBlockedJwtEndpoint(request: Request): boolean {
  const rawPathname = new URL(request.url).pathname;
  let pathname: string;
  try {
    pathname = decodeURIComponent(rawPathname).replace(/\/+$/, "");
  } catch {
    // Malformed encoded paths are not valid auth requests. Keep them out of
    // the Better Auth router instead of relying on framework-specific decode
    // behavior.
    return true;
  }
  return BLOCKED_JWT_ENDPOINTS.has(pathname);
}

function jwtEndpointNotFound(): Response {
  // JWT signing/verification is a server-internal capability. Returning a
  // non-enumerating 404 keeps the plugin mounted for `auth.api.getToken` while
  // ensuring browsers cannot mint or inspect execution tokens directly.
  return NextResponse.json({ error: "not_found" }, { status: 404 });
}

export async function GET(request: Request): Promise<Response> {
  if (isBlockedJwtEndpoint(request)) return jwtEndpointNotFound();
  return authHandler.GET(request);
}

export async function POST(request: Request): Promise<Response> {
  if (isBlockedJwtEndpoint(request)) return jwtEndpointNotFound();
  return authHandler.POST(request);
}
