import type { ApiErrorResponse, ApiResponse } from "@/types";

// ── ApiError ──

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// ── Base URL ──

const isServer = typeof window === "undefined";

function getBaseUrl(): string {
  if (isServer) {
    return `${process.env.API_URL}/api/v1`;
  }
  return "/api/proxy";
}

// ── Request Options ──

interface RequestOptions {
  params?: Record<string, string | undefined>;
  headers?: Record<string, string>;
  body?: unknown;
}

// ── Internal request helper ──

async function request<T>(
  method: string,
  path: string,
  options?: RequestOptions,
): Promise<T> {
  const url = new URL(
    `${getBaseUrl()}${path}`,
    isServer ? undefined : window.location.origin,
  );

  if (options?.params) {
    for (const [key, value] of Object.entries(options.params)) {
      if (value !== undefined) {
        url.searchParams.set(key, value);
      }
    }
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...options?.headers,
  };

  if (isServer) {
    try {
      const { cookies } = await import("next/headers");
      const token = (await cookies()).get("auth_token")?.value;
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }
    } catch {
      // Outside Next.js request context (e.g., tests) — skip auth
    }
  }

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: options?.body ? JSON.stringify(options.body) : undefined,
  });

  if (!res.ok) {
    let code = "UNKNOWN";
    let message = `HTTP ${res.status}`;

    try {
      const errorBody = (await res.json()) as ApiErrorResponse;
      if (errorBody.error) {
        code = errorBody.error.code;
        message = errorBody.error.message;
      }
    } catch {
      // Non-JSON error response — use generic message
    }

    throw new ApiError(res.status, code, message);
  }

  // 204 No Content
  if (res.status === 204) {
    return undefined as T;
  }

  return (await res.json()) as T;
}

// ── Public API ──

/** GET single resource — auto-unwraps `{ "data": T }` → `T` */
async function get<T>(path: string, options?: RequestOptions): Promise<T> {
  const response = await request<ApiResponse<T>>("GET", path, options);
  return response.data;
}

/** GET list — returns full response (preserves pagination) */
async function getList<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>("GET", path, options);
}

/** POST — auto-unwraps `{ "data": T }` → `T` */
async function post<T>(
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  const response = await request<ApiResponse<T>>("POST", path, {
    ...options,
    body,
  });
  return response.data;
}

/** PUT — auto-unwraps `{ "data": T }` → `T` */
async function put<T>(
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  const response = await request<ApiResponse<T>>("PUT", path, {
    ...options,
    body,
  });
  return response.data;
}

/** DELETE — returns T (handles 204 No Content gracefully) */
async function del<T = void>(
  path: string,
  options?: RequestOptions,
): Promise<T> {
  const response = await request<ApiResponse<T> | undefined>("DELETE", path, options);
  if (!response) return undefined as T;
  return response.data;
}

export const apiClient = { get, getList, post, put, del };
