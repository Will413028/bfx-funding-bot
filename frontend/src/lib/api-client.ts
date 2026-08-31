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
    ...options?.headers,
  };
  if (options?.body) {
    headers["Content-Type"] = "application/json";
  }

  if (isServer) {
    try {
      const { auth, getOperatorMfaSessionAccess } = await import("@/lib/auth");
      const { headers: nextHeaders } = await import("next/headers");
      const requestHeaders = await nextHeaders();
      const operatorUserId = process.env.BFX_OPERATOR_USER_ID?.trim();
      if (!operatorUserId) {
        throw new ApiError(
          503,
          "auth_not_configured",
          "Operator authorization is not configured",
        );
      }
      const access = await getOperatorMfaSessionAccess(
        requestHeaders,
        operatorUserId,
      );
      if (!access.allowed) {
        throw new ApiError(403, access.error, access.error);
      }
      const res = await auth.api.getToken({ headers: requestHeaders });
      if (res?.token) headers.Authorization = `Bearer ${res.token}`;
      else {
        throw new ApiError(401, "UNAUTHORIZED", "Auth token unavailable");
      }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      // A missing request context or unavailable auth dependency is not a
      // reason to send an unauthenticated request to a private backend route.
      throw new ApiError(
        403,
        "operator_required",
        "Operator session unavailable",
      );
    }
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
  const response = await request<ApiResponse<T> | undefined>(
    "DELETE",
    path,
    options,
  );
  if (!response) return undefined as T;
  return response.data;
}

export const apiClient = { get, getList, post, put, del };
