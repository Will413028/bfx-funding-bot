import { describe, expect, it } from "vitest";
import { routing } from "@/i18n/routing";
import { ApiError } from "@/lib/api-client";
import { makeQueryClient } from "../query-provider";

describe("makeQueryClient", () => {
  it("drops every cached answer when a request comes back 401", async () => {
    const client = makeQueryClient();
    client.setQueryData(["balances"], { usd: 1 });
    await client
      .fetchQuery({
        queryKey: ["profile"],
        retry: false,
        queryFn: () =>
          Promise.reject(new ApiError(401, "operator_required", "x")),
      })
      .catch(() => undefined);
    expect(client.getQueryData(["balances"])).toBeUndefined();
  });

  it("keeps the cache on any other error", async () => {
    const client = makeQueryClient();
    client.setQueryData(["balances"], { usd: 1 });
    await client
      .fetchQuery({
        queryKey: ["profile"],
        retry: false,
        queryFn: () => Promise.reject(new ApiError(503, "unavailable", "x")),
      })
      .catch(() => undefined);
    expect(client.getQueryData(["balances"])).toEqual({ usd: 1 });
  });

  it("drops the cache when a mutation comes back 401", async () => {
    const client = makeQueryClient();
    client.setQueryData(["balances"], { usd: 1 });
    await client
      .getMutationCache()
      .build(client, {
        mutationFn: () =>
          Promise.reject(new ApiError(401, "operator_required", "x")),
      })
      .execute(undefined)
      .catch(() => undefined);
    expect(client.getQueryData(["balances"])).toBeUndefined();
  });
});

describe("routing", () => {
  it("writes no locale cookie", () => {
    expect(routing.localeCookie).toBe(false);
  });
});
