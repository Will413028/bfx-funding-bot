import { cookies } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;

async function proxyRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const targetPath = `/api/v1/${path.join("/")}`;
  const url = new URL(targetPath, API_URL);

  // Guard against path-traversal: resolved pathname must stay under /api/v1/
  if (!url.pathname.startsWith("/api/v1/")) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  // Preserve query parameters
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  // Read auth token from HttpOnly cookie
  const token = (await cookies()).get("auth_token")?.value;

  const headers = new Headers();
  if (request.method !== "GET" && request.method !== "DELETE") {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(url.toString(), {
    method: request.method,
    headers,
    body: request.method !== "GET" && request.method !== "DELETE" ? await request.text() : undefined,
  });

  const data = await res.text();
  return new NextResponse(data, {
    status: res.status,
    headers: { "Content-Type": res.headers.get("Content-Type") ?? "application/json" },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const DELETE = proxyRequest;
