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

/** Every API call goes to the web API that serves this UI (same origin, behind Serve). */
const API_BASE = "/api/v1";

const UUID_PATH_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** Build a private API path from the only accepted account identity. */
export function accountScopedPath(
  exchangeAccountId: string,
  resourcePath: `/${string}`,
): string {
  const canonical = exchangeAccountId.trim().toLowerCase();
  if (!UUID_PATH_RE.test(canonical)) {
    throw new Error("exchangeAccountId must be a canonical UUID");
  }
  return `/exchange-accounts/${canonical}${resourcePath}`;
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
  const url = new URL(`${API_BASE}${path}`, window.location.origin);

  if (options?.params) {
    for (const [key, value] of Object.entries(options.params)) {
      if (value !== undefined) {
        url.searchParams.set(key, value);
      }
    }
  }

  const headers: Record<string, string> = {
    ...options?.headers,
  };
  if (options?.body) {
    headers["Content-Type"] = "application/json";
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: options?.body ? JSON.stringify(options.body) : undefined,
    signal: controller.signal,
  });

  clearTimeout(timeout);

  if (!res.ok) {
    let code = "UNKNOWN";
    let message = `HTTP ${res.status}`;

    try {
      const errorBody = (await res.json()) as Partial<ApiErrorResponse> & {
        detail?: unknown;
      };
      if (errorBody.error) {
        code = errorBody.error.code;
        message = errorBody.error.message;
      } else if (typeof errorBody.detail === "string") {
        // FastAPI's HTTPException shape: the detail is a bounded reason code.
        code = errorBody.detail;
        message = errorBody.detail;
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
  const response = await request<ApiResponse<T> | undefined>(
    "DELETE",
    path,
    options,
  );
  if (!response) return undefined as T;
  return response.data;
}

export const apiClient = { get, getList, post, put, del, accountScopedPath };
