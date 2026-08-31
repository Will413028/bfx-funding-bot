import { describe, expect, it } from "vitest";
import {
  apiKeyKeys,
  configKeys,
  exchangeAccountKeys,
  executionKeys,
  offerKeys,
  positionKeys,
} from "../query-keys";

describe("apiKeyKeys", () => {
  it("all returns base key", () => {
    expect(apiKeyKeys.all).toEqual(["api-keys"]);
  });

  it("list extends all", () => {
    expect(apiKeyKeys.list("account-1")).toEqual([
      "api-keys",
      "account-1",
      "list",
    ]);
  });
});

describe("configKeys", () => {
  it("current extends all", () => {
    expect(configKeys.current("account-1")).toEqual([
      "configs",
      "account-1",
      "current",
    ]);
  });
});

describe("positionKeys", () => {
  it("list extends all", () => {
    expect(positionKeys.list("account-1")).toEqual([
      "positions",
      "account-1",
      "list",
    ]);
  });
});

describe("offerKeys", () => {
  it("list includes state param", () => {
    expect(offerKeys.list("account-1", "released")).toEqual([
      "offers",
      "account-1",
      "list",
      "released",
    ]);
  });

  it("list without state includes undefined", () => {
    expect(offerKeys.list("account-1")).toEqual([
      "offers",
      "account-1",
      "list",
      undefined,
    ]);
  });
});

describe("executionKeys", () => {
  it("events without filter uses the all bucket", () => {
    expect(executionKeys.events("account-1")).toEqual([
      "executions",
      "account-1",
      "events",
      "all",
    ]);
  });

  it("events includes the event-type filter", () => {
    expect(executionKeys.events("account-1", "ORDER_FILL")).toEqual([
      "executions",
      "account-1",
      "events",
      "ORDER_FILL",
    ]);
  });
});

describe("exchangeAccountKeys", () => {
  it("keeps bootstrap account discovery separate from scoped resources", () => {
    expect(exchangeAccountKeys.list()).toEqual(["exchange-accounts", "list"]);
  });
});
