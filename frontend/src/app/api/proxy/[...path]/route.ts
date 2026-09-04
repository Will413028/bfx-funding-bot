import { headers } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";
import { auth, getOperatorMfaSessionAccess } from "@/lib/auth";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;
const PUBLIC_PROXY_PATHS = new Set([
  "public/proof-summary",
  "public/funding-rates.csv",
]);

function canonicalProxyPath(path: string[]): string | null {
  if (
    path.length === 0 ||
    path.some(
      (segment) =>
        segment.length === 0 ||
        segment === "." ||
        segment === ".." ||
        segment.includes("/") ||
        segment.includes("\\") ||
        segment.includes("%") ||
        segment.includes("?") ||
        segment.includes("#"),
    )
  ) {
    return null;
  }

  const joined = path.join("/");
  const url = new URL(`/api/v1/${joined}`, API_URL);
  const canonical = url.pathname.slice("/api/v1/".length);
  return canonical === joined ? canonical : null;
}

async function proxyRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const canonicalPath = canonicalProxyPath(path);
  if (canonicalPath === null) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }
  const url = new URL(`/api/v1/${canonicalPath}`, API_URL);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  const body =
    request.method !== "GET" && request.method !== "DELETE"
      ? await request.text()
      : undefined;

  // Public marketing data has an exact allowlist and never touches the
  // operator/MFA/JWT path. Everything else remains private by default.
  if (PUBLIC_PROXY_PATHS.has(canonicalPath)) {
    if (request.method !== "GET") {
      return NextResponse.json(
        { error: "method_not_allowed" },
        { status: 405 },
      );
    }
    const res = await fetch(url.toString(), {
      method: "GET",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
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

  const operatorUserId = process.env.BFX_OPERATOR_USER_ID?.trim();
  if (!operatorUserId) {
    return NextResponse.json({ error: "auth_not_configured" }, { status: 503 });
  }

  const requestHeaders = await headers();
  const access = await getOperatorMfaSessionAccess(
    requestHeaders,
    operatorUserId,
  );
  if (!access.allowed) {
    return NextResponse.json({ error: access.error }, { status: 403 });
  }

  // Mint a short-lived EdDSA JWT for the current session (browser never holds it).
  let token: string | undefined;
  try {
    const res = await auth.api.getToken({ headers: requestHeaders });
    token = res?.token;
  } catch {
    token = undefined;
  }
  if (!token) {
    return NextResponse.json(
      { error: "auth_token_unavailable" },
      { status: 503 },
    );
  }

  const fwd = new Headers();
  if (body !== undefined) fwd.set("Content-Type", "application/json");
  fwd.set("Authorization", `Bearer ${token}`);

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
