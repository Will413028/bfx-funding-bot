import { describe, expect, it } from "vitest";
import {
  apiKeyKeys,
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

describe("executionKeys", () => {
  it("events without filter uses the all bucket", () => {
    expect(executionKeys.events()).toEqual(["executions", "events", "all"]);
  });

  it("events includes the event-type filter", () => {
    expect(executionKeys.events("ORDER_FILL")).toEqual([
      "executions",
      "events",
      "ORDER_FILL",
    ]);
  });
});
