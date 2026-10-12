import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { getSchema } from "better-auth/db";
import { describe, expect, it } from "vitest";
import { auth } from "../auth";

// The auth tables are created by backend Alembic migrations, not by Better Auth. This file
// is the contract between the two: the columns the installed Better Auth (with this app's
// plugins and storage) reads and writes. backend/tests/integration/test_auth_schema_contract.py
// checks that the migrated `auth` schema has every one of them.
const contractPath = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../auth-db-schema.json",
);

function requiredColumns(): Record<string, string[]> {
  const schema = getSchema(auth.options) as Record<
    string,
    { modelName?: string; fields: Record<string, { fieldName?: string }> }
  >;
  return Object.fromEntries(
    Object.entries(schema)
      .map(([key, model]): [string, string[]] => [
        model.modelName ?? key,
        Object.entries(model.fields)
          .map(([name, field]) => field.fieldName ?? name)
          .sort(),
      ])
      .sort(([a], [b]) => a.localeCompare(b)),
  );
}

describe("auth DB schema contract", () => {
  it("matches the columns the installed Better Auth needs", () => {
    const contract = JSON.parse(readFileSync(contractPath, "utf8"));
    // On a Better Auth upgrade or plugin change: update auth-db-schema.json to the
    // received value and add the Alembic migration that creates the new columns.
    expect(requiredColumns()).toEqual(contract);
  });
});
