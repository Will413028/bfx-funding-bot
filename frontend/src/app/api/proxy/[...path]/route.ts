import { headers } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;

async function proxyRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const url = new URL(`/api/v1/${path.join("/")}`, API_URL);
  if (!url.pathname.startsWith("/api/v1/")) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  const body =
    request.method !== "GET" && request.method !== "DELETE"
      ? await request.text()
      : undefined;

  const operatorUserId = process.env.BFX_OPERATOR_USER_ID?.trim();
  if (!operatorUserId) {
    return NextResponse.json({ error: "auth_not_configured" }, { status: 503 });
  }

  const requestHeaders = await headers();
  let session: Awaited<ReturnType<typeof auth.api.getSession>> | null = null;
  try {
    session = await auth.api.getSession({ headers: requestHeaders });
  } catch {
    session = null;
  }

  if (session?.user.id !== operatorUserId) {
    return NextResponse.json({ error: "operator_required" }, { status: 403 });
  }

  // Better Auth 1.6.14 does not expose a `twoFactorVerified` session field.
  // Its two-factor plugin deletes the sign-in session for an enrolled user and
  // creates the usable session only after server-side verification succeeds.
  // A returned session plus this server-owned user flag is therefore the typed
  // post-MFA boundary; client-provided state is never consulted.
  if (session.user.twoFactorEnabled !== true) {
    return NextResponse.json({ error: "mfa_required" }, { status: 403 });
  }

  // Mint a short-lived EdDSA JWT for the current session (browser never holds it).
  let token: string | undefined;
  try {
    const res = await auth.api.getToken({ headers: requestHeaders });
    token = res?.token;
  } catch {
    token = undefined;
  }

  const fwd = new Headers();
  if (body !== undefined) fwd.set("Content-Type", "application/json");
  if (token) fwd.set("Authorization", `Bearer ${token}`);

  const res = await fetch(url.toString(), {
    method: request.method,
    headers: fwd,
    body,
  });
  const data = await res.text();
  return new NextResponse(data, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "application/json",
    },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const DELETE = proxyRequest;
