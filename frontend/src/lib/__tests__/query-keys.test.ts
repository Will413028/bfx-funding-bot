import { describe, expect, it } from "vitest";
import {
  apiKeyKeys,
  billingKeys,
  configKeys,
  executionKeys,
  offerKeys,
  positionKeys,
} from "../query-keys";

describe("apiKeyKeys", () => {
  it("all returns base key", () => {
    expect(apiKeyKeys.all).toEqual(["api-keys"]);
  });

  it("list extends all", () => {
    expect(apiKeyKeys.list()).toEqual(["api-keys", "list"]);
  });
});

describe("configKeys", () => {
  it("current extends all", () => {
    expect(configKeys.current()).toEqual(["configs", "current"]);
  });
});

describe("positionKeys", () => {
  it("list extends all", () => {
    expect(positionKeys.list()).toEqual(["positions", "list"]);
  });
});

describe("offerKeys", () => {
  it("list includes state param", () => {
    expect(offerKeys.list("released")).toEqual(["offers", "list", "released"]);
  });

  it("list without state includes undefined", () => {
    expect(offerKeys.list()).toEqual(["offers", "list", undefined]);
  });
});

describe("billingKeys", () => {
  it("list includes params", () => {
    const params = { status: "paid" };
    expect(billingKeys.list(params)).toEqual(["billing", "list", params]);
  });

  it("plan extends all", () => {
    expect(billingKeys.plan()).toEqual(["billing", "plan"]);
  });
});

describe("executionKeys", () => {
  it("list includes params", () => {
    const params = { after: "abc" };
    expect(executionKeys.list(params)).toEqual(["executions", "list", params]);
  });

  it("events extends all", () => {
    expect(executionKeys.events()).toEqual(["executions", "events"]);
  });
});
