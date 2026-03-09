import { describe, expect, it } from "vitest";
import {
  apiKeyKeys,
  billingKeys,
  configKeys,
  dashboardKeys,
  earningsKeys,
  executionKeys,
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

describe("dashboardKeys", () => {
  it("summary extends all", () => {
    expect(dashboardKeys.summary()).toEqual(["dashboard", "summary"]);
  });
});

describe("earningsKeys", () => {
  it("summary extends all", () => {
    expect(earningsKeys.summary()).toEqual(["earnings", "summary"]);
  });

  it("history includes days param", () => {
    expect(earningsKeys.history(30)).toEqual(["earnings", "history", 30]);
  });

  it("history without days includes undefined", () => {
    expect(earningsKeys.history()).toEqual(["earnings", "history", undefined]);
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
});
