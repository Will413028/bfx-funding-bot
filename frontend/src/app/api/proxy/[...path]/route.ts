import { cookies } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;
const IS_PRODUCTION = process.env.NODE_ENV === "production";

const ACCESS_TOKEN_MAX_AGE = 15 * 60;
const REFRESH_TOKEN_MAX_AGE = 60 * 60 * 24 * 7;

// Dedup concurrent refresh requests within the same process
let refreshPromise: Promise<string | null> | null = null;

async function proxyRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const targetPath = `/api/v1/${path.join("/")}`;
  const url = new URL(targetPath, API_URL);

  // Guard against path-traversal
  if (!url.pathname.startsWith("/api/v1/")) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  // Preserve query parameters
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  // Read request body once (for retry)
  const body =
    request.method !== "GET" && request.method !== "DELETE"
      ? await request.text()
      : undefined;

  // First attempt
  let res = await doFetch(url, request.method, body);

  // If 401 and we have a refresh token, try to refresh
  if (res.status === 401) {
    const newToken = await tryRefreshDedup();
    if (newToken) {
      // Retry with new access token
      res = await doFetch(url, request.method, body, newToken);
    }
  }

  const data = await res.text();
  return new NextResponse(data, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "application/json",
    },
  });
}

async function doFetch(
  url: URL,
  method: string,
  body: string | undefined,
  overrideToken?: string,
): Promise<Response> {
  const token = overrideToken ?? (await cookies()).get("auth_token")?.value;

  const headers = new Headers();
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  return fetch(url.toString(), {
    method,
    headers,
    body,
  });
}

async function tryRefreshDedup(): Promise<string | null> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = tryRefresh().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function tryRefresh(): Promise<string | null> {
  const jar = await cookies();
  const refreshToken = jar.get("refresh_token")?.value;
  if (!refreshToken) {
    return null;
  }

  try {
    const res = await fetch(`${API_URL}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refreshToken }),
    });

    if (!res.ok) {
      // Refresh failed — clear cookies
      jar.delete("auth_token");
      jar.delete({ name: "refresh_token", path: "/api" });
      return null;
    }

    const data = (await res.json()) as {
      data: { accessToken: string; refreshToken: string };
    };

    // Set new cookies
    jar.set("auth_token", data.data.accessToken, {
      httpOnly: true,
      secure: IS_PRODUCTION,
      sameSite: "lax",
      path: "/",
      maxAge: ACCESS_TOKEN_MAX_AGE,
    });
    jar.set("refresh_token", data.data.refreshToken, {
      httpOnly: true,
      secure: IS_PRODUCTION,
      sameSite: "strict",
      path: "/api",
      maxAge: REFRESH_TOKEN_MAX_AGE,
    });

    return data.data.accessToken;
  } catch {
    return null;
  }
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const DELETE = proxyRequest;
