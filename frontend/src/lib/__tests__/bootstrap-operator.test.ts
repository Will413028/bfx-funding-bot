import { describe, expect, it } from "vitest";

import { main } from "../../../scripts/bootstrap-operator.mjs";

const OPERATOR_ID = "operator-user";
const OTHER_ID = "other-user";

type UserRow = {
  id: string;
  role: string | null;
  banned: boolean | null;
  twoFactorEnabled: boolean | null;
};

type AccountRow = {
  id: string;
  userId: string;
  accountId: string;
  providerId: string;
  hasPassword: boolean;
};

type TwoFactorRow = {
  id: string;
  userId: string;
  verified: boolean | null;
};

function successfulEnv(overrides: Record<string, string | undefined> = {}) {
  return {
    DATABASE_URL: "postgresql://fixture.invalid/auth",
    REDIS_URL: "redis://fixture.invalid/0",
    BFX_OPERATOR_USER_ID: OPERATOR_ID,
    BFX_OPERATOR_ROLE: "admin",
    ...overrides,
  };
}

function makeFixture(
  overrides: {
    users?: UserRow[];
    accounts?: AccountRow[];
    twoFactors?: TwoFactorRow[];
    redis?: Map<string, string>;
    reserveError?: string;
    finalAuditError?: string;
    redisGetFailures?: number;
    redisDeleteFailureCall?: number;
    commitAcknowledgementFailures?: number;
  } = {},
) {
  const users = structuredClone(
    overrides.users ?? [
      {
        id: OPERATOR_ID,
        role: "user",
        banned: false,
        twoFactorEnabled: true,
      },
      {
        id: OTHER_ID,
        role: "user",
        banned: false,
        twoFactorEnabled: false,
      },
    ],
  );
  const accounts = structuredClone(
    overrides.accounts ?? [
      {
        id: "credential-1",
        userId: OPERATOR_ID,
        accountId: OPERATOR_ID,
        providerId: "credential",
        hasPassword: true,
      },
    ],
  );
  const twoFactors = structuredClone(
    overrides.twoFactors ?? [
      {
        id: "totp-1",
        userId: OPERATOR_ID,
        verified: true,
      },
    ],
  );
  const redis =
    overrides.redis ??
    new Map([
      [
        `active-sessions-${OPERATOR_ID}`,
        JSON.stringify([
          { token: "operator-session", expiresAt: 4_102_444_800_000 },
        ]),
      ],
      ["operator-session", "operator-session-body"],
      ["bfx:mfa-verified:operator-session", "1"],
      [`active-sessions-${OTHER_ID}`, JSON.stringify([])],
      ["unrelated-key", "keep-me"],
    ]);
  const events: string[] = [];
  const queries: string[] = [];
  const auditWrites: Array<Record<string, unknown>> = [];
  const stderr: string[] = [];
  const stdout: string[] = [];
  const changedRoles: Array<{
    id: string;
    before: string | null;
    after: string;
  }> = [];
  let redisGetFailures = overrides.redisGetFailures ?? 0;
  let commitAcknowledgementFailures =
    overrides.commitAcknowledgementFailures ?? 0;
  let redisDeleteCalls = 0;
  let inTransaction = false;
  let snapshots: UserRow[] | undefined;

  const client = {
    async query(sql: string, parameters: unknown[] = []) {
      const normalized = sql.replace(/\s+/g, " ").trim();
      queries.push(normalized);
      events.push(`sql:${normalized.split(" ").slice(0, 3).join(" ")}`);
      if (normalized === "BEGIN" || normalized === "BEGIN READ ONLY") {
        inTransaction = true;
        snapshots = structuredClone(users);
        return { rowCount: null, rows: [] };
      }
      if (normalized === "COMMIT") {
        inTransaction = false;
        snapshots = undefined;
        if (commitAcknowledgementFailures > 0) {
          commitAcknowledgementFailures -= 1;
          throw new Error("synthetic COMMIT acknowledgement failure");
        }
        return { rowCount: null, rows: [] };
      }
      if (normalized === "ROLLBACK") {
        if (inTransaction && snapshots)
          users.splice(0, users.length, ...snapshots);
        inTransaction = false;
        snapshots = undefined;
        return { rowCount: null, rows: [] };
      }
      if (normalized.includes('FROM auth."user"')) {
        return { rowCount: users.length, rows: structuredClone(users) };
      }
      if (normalized.includes('FROM auth."account"')) {
        const rows = accounts.filter(
          (row) =>
            row.userId === parameters[0] && row.providerId === "credential",
        );
        return { rowCount: rows.length, rows: structuredClone(rows) };
      }
      if (normalized.includes('FROM auth."twoFactor"')) {
        const rows = twoFactors.filter((row) => row.userId === parameters[0]);
        return { rowCount: rows.length, rows: structuredClone(rows) };
      }
      if (normalized.startsWith('UPDATE auth."user"')) {
        const user = users.find((row) => row.id === parameters[0]);
        if (!user) return { rowCount: 0, rows: [] };
        const before = user.role;
        user.role = "admin";
        changedRoles.push({ id: user.id, before, after: "admin" });
        return { rowCount: 1, rows: [] };
      }
      return { rowCount: null, rows: [] };
    },
    release() {
      events.push("db:release");
    },
  };

  const pool = {
    async connect() {
      events.push("db:connect");
      return client;
    },
    async end() {
      events.push("db:end");
    },
  };

  const redisClient = {
    async connect() {
      events.push("redis:connect");
    },
    async get(key: string) {
      events.push(`redis:get:${key}`);
      if (redisGetFailures > 0) {
        redisGetFailures -= 1;
        throw new Error("redis://secret.invalid leaked-token");
      }
      return redis.get(key) ?? null;
    },
    async del(...keys: string[]) {
      redisDeleteCalls += 1;
      events.push(`redis:del:${keys.join("|")}`);
      if (redisDeleteCalls === overrides.redisDeleteFailureCall) {
        throw new Error("synthetic Redis batch failure");
      }
      let deleted = 0;
      for (const key of keys) {
        if (redis.delete(key)) deleted += 1;
      }
      return deleted;
    },
    async quit() {
      events.push("redis:quit");
    },
    disconnect() {
      events.push("redis:disconnect");
    },
  };

  const dependencies = {
    randomUUID: () => "00000000-0000-4000-8000-000000000001",
    now: () => new Date("2026-09-20T00:00:00.000Z"),
    createPool: () => pool,
    createRedis: () => redisClient,
    reserveAudit: async (_path: string, initial: Record<string, unknown>) => {
      events.push("audit:reserve");
      if (overrides.reserveError) throw new Error(overrides.reserveError);
      auditWrites.push(structuredClone(initial));
      return {
        async write(record: Record<string, unknown>) {
          events.push("audit:write");
          if (overrides.finalAuditError) {
            throw new Error(overrides.finalAuditError);
          }
          auditWrites.push(structuredClone(record));
        },
        async close() {
          events.push("audit:close");
        },
      };
    },
    stdout: (line: string) => stdout.push(line),
    stderr: (line: string) => stderr.push(line),
  };

  return {
    dependencies,
    users,
    accounts,
    twoFactors,
    redis,
    events,
    queries,
    auditWrites,
    stderr,
    stdout,
    changedRoles: () => changedRoles,
  };
}

describe("audited first-admin bootstrap", () => {
  it("defaults to a read-only dry run and changes no role or Redis key", async () => {
    const fixture = makeFixture();

    const result = await main([], successfulEnv(), fixture.dependencies);

    expect(result).toMatchObject({
      mode: "dry-run",
      status: "completed",
      operatorUserId: OPERATOR_ID,
      dbCommitted: false,
      roleChanged: false,
      activeSessionCount: 1,
      revokedSessionCount: 0,
      revokedKeyCount: 0,
    });
    expect(fixture.changedRoles()).toEqual([]);
    expect(fixture.queries).toContain("BEGIN READ ONLY");
    expect(
      fixture.queries.some((query) => query.startsWith("LOCK TABLE")),
    ).toBe(false);
    expect(fixture.queries.some((query) => query.startsWith("UPDATE"))).toBe(
      false,
    );
    expect(fixture.events.some((event) => event.startsWith("redis:del:"))).toBe(
      false,
    );
  });

  it.each([
    ["DATABASE_URL", { DATABASE_URL: undefined }, "missing_configuration"],
    ["REDIS_URL", { REDIS_URL: undefined }, "missing_configuration"],
    [
      "BFX_OPERATOR_USER_ID",
      { BFX_OPERATOR_USER_ID: undefined },
      "missing_configuration",
    ],
    ["admin role", { BFX_OPERATOR_ROLE: "user" }, "invalid_operator_role"],
  ])(
    "rejects missing or invalid %s before opening the database",
    async (_, env, code) => {
      const fixture = makeFixture();

      const result = await main([], successfulEnv(env), fixture.dependencies);

      expect(result).toMatchObject({ status: "failed", failureCode: code });
      expect(fixture.events).not.toContain("db:connect");
    },
  );

  it("requires an optional CLI operator ID to equal the configured ID", async () => {
    const fixture = makeFixture();

    const result = await main(
      ["--operator-id", OTHER_ID],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({
      status: "failed",
      failureCode: "operator_mismatch",
      operatorUserId: OPERATOR_ID,
    });
    expect(fixture.events).not.toContain("db:connect");
  });

  it.each([
    [[], "invalid_arguments"],
    [["--confirm-bootstrap-admin"], "invalid_arguments"],
    [["--audit-file", "/audit/only.json"], "invalid_arguments"],
  ])("requires both apply confirmations (%j)", async (tail, code) => {
    const fixture = makeFixture();

    const result = await main(
      ["--apply", ...tail],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({ status: "failed", failureCode: code });
    expect(fixture.events).not.toContain("audit:reserve");
    expect(fixture.events).not.toContain("db:connect");
  });

  it.each([
    ["missing", [], "operator_not_found"],
    [
      "ambiguous",
      [
        {
          id: OPERATOR_ID,
          role: "user",
          banned: false,
          twoFactorEnabled: true,
        },
        {
          id: OPERATOR_ID,
          role: "user",
          banned: false,
          twoFactorEnabled: true,
        },
      ],
      "operator_ambiguous",
    ],
    [
      "banned",
      [{ id: OPERATOR_ID, role: "user", banned: true, twoFactorEnabled: true }],
      "operator_banned",
    ],
  ])("rejects a %s configured user before UPDATE", async (_, users, code) => {
    const fixture = makeFixture({ users });

    const result = await main([], successfulEnv(), fixture.dependencies);

    expect(result).toMatchObject({ status: "failed", failureCode: code });
    expect(fixture.changedRoles()).toEqual([]);
    expect(fixture.queries.some((query) => query.startsWith("UPDATE"))).toBe(
      false,
    );
  });

  it.each([
    [
      "twoFactorEnabled is false",
      {
        users: [
          {
            id: OPERATOR_ID,
            role: "user",
            banned: false,
            twoFactorEnabled: false,
          },
        ],
      },
      "operator_mfa_not_enabled",
    ],
    ["credential is absent", { accounts: [] }, "credential_not_found"],
    [
      "credential is duplicated",
      {
        accounts: [
          {
            id: "credential-1",
            userId: OPERATOR_ID,
            accountId: OPERATOR_ID,
            providerId: "credential",
            hasPassword: true,
          },
          {
            id: "credential-2",
            userId: OPERATOR_ID,
            accountId: OPERATOR_ID,
            providerId: "credential",
            hasPassword: true,
          },
        ],
      },
      "credential_ambiguous",
    ],
    ["verified TOTP is absent", { twoFactors: [] }, "verified_totp_not_found"],
    [
      "TOTP row is unverified",
      {
        twoFactors: [{ id: "totp-1", userId: OPERATOR_ID, verified: false }],
      },
      "verified_totp_not_found",
    ],
    [
      "TOTP row is duplicated",
      {
        twoFactors: [
          { id: "totp-1", userId: OPERATOR_ID, verified: true },
          { id: "totp-2", userId: OPERATOR_ID, verified: true },
        ],
      },
      "verified_totp_ambiguous",
    ],
  ])("rejects when %s before UPDATE", async (_, overrides, code) => {
    const fixture = makeFixture(overrides);

    const result = await main([], successfulEnv(), fixture.dependencies);

    expect(result).toMatchObject({ status: "failed", failureCode: code });
    expect(fixture.changedRoles()).toEqual([]);
  });

  it.each([
    ["other exact admin", "admin"],
    ["other comma-separated admin", "user, admin"],
    ["target comma-separated admin", "admin,user"],
  ])("rejects %s as conflicting authority", async (kind, role) => {
    const users =
      kind === "target comma-separated admin"
        ? [
            {
              id: OPERATOR_ID,
              role,
              banned: false,
              twoFactorEnabled: true,
            },
          ]
        : [
            {
              id: OPERATOR_ID,
              role: "user",
              banned: false,
              twoFactorEnabled: true,
            },
            {
              id: OTHER_ID,
              role,
              banned: false,
              twoFactorEnabled: false,
            },
          ];
    const fixture = makeFixture({ users });

    const result = await main([], successfulEnv(), fixture.dependencies);

    expect(result).toMatchObject({
      status: "failed",
      failureCode: "admin_conflict",
    });
    expect(fixture.changedRoles()).toEqual([]);
  });

  it("reserves audit first, commits one exact role, and revokes only operator sessions", async () => {
    const fixture = makeFixture();

    const result = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/apply.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({
      status: "completed",
      operatorUserId: OPERATOR_ID,
      dbCommitted: true,
      roleChanged: true,
      activeSessionCount: 1,
      revokedSessionCount: 1,
      revokedKeyCount: 3,
    });
    expect(fixture.events.indexOf("audit:reserve")).toBeLessThan(
      fixture.events.indexOf("db:connect"),
    );
    expect(fixture.queries).toContain(
      'LOCK TABLE auth."user" IN SHARE ROW EXCLUSIVE MODE',
    );
    expect(fixture.changedRoles()).toEqual([
      { id: OPERATOR_ID, before: "user", after: "admin" },
    ]);
    expect(fixture.redis.has(`active-sessions-${OPERATOR_ID}`)).toBe(false);
    expect(fixture.redis.has("operator-session")).toBe(false);
    expect(fixture.redis.has("bfx:mfa-verified:operator-session")).toBe(false);
    expect(fixture.redis.get(`active-sessions-${OTHER_ID}`)).toBe("[]");
    expect(fixture.redis.get("unrelated-key")).toBe("keep-me");
    expect(fixture.auditWrites.at(-1)).toEqual(result);
  });

  it("is idempotent when the configured operator is already the sole exact admin", async () => {
    const fixture = makeFixture({
      users: [
        {
          id: OPERATOR_ID,
          role: "admin",
          banned: false,
          twoFactorEnabled: true,
        },
      ],
    });

    const result = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/retry.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({
      status: "completed",
      dbCommitted: true,
      roleChanged: false,
    });
    expect(fixture.changedRoles()).toEqual([]);
  });

  it("records an unknown database outcome when COMMIT applies before acknowledgement is lost", async () => {
    const fixture = makeFixture({ commitAcknowledgementFailures: 1 });

    const first = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/commit-unknown.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(first).toMatchObject({
      status: "outcome_unknown",
      failureCode: "database_outcome_unknown",
      transactionPhase: "commit_acknowledgement_unknown",
      databaseOutcome: "unknown",
      dbCommitted: null,
      roleChanged: null,
      reconciliationRequired: true,
    });
    expect(fixture.users.find((row) => row.id === OPERATOR_ID)?.role).toBe(
      "admin",
    );
    expect(fixture.events.some((event) => event.startsWith("redis:"))).toBe(
      false,
    );
    expect(fixture.auditWrites.at(-1)).toEqual(first);

    const retry = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/commit-retry.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(retry).toMatchObject({
      status: "completed",
      databaseOutcome: "committed",
      dbCommitted: true,
      roleChanged: false,
      reconciliationRequired: false,
      revokedSessionCount: 1,
    });
  });

  it("fails closed on malformed session inventory without deleting any Redis key", async () => {
    const redis = new Map([
      [`active-sessions-${OPERATOR_ID}`, '[{"token":"missing-expiry"}]'],
      ["missing-expiry", "keep"],
      ["bfx:mfa-verified:missing-expiry", "keep"],
    ]);
    const fixture = makeFixture({ redis });

    const result = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/malformed.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({
      status: "partial_failure",
      dbCommitted: true,
      failureCode: "invalid_session_inventory",
      revokedKeyCount: 0,
    });
    expect(redis.get("missing-expiry")).toBe("keep");
    expect(redis.get("bfx:mfa-verified:missing-expiry")).toBe("keep");
    expect(fixture.events.some((event) => event.startsWith("redis:del:"))).toBe(
      false,
    );
  });

  it("records a Redis partial failure and safely retries the same sole admin", async () => {
    const fixture = makeFixture({ redisGetFailures: 1 });
    const first = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/first.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(first).toMatchObject({
      status: "partial_failure",
      failureCode: "redis_operation_failed",
      dbCommitted: true,
      roleChanged: true,
    });
    expect(JSON.stringify(first)).not.toContain("leaked-token");
    expect(fixture.stderr.join("\n")).not.toContain("secret.invalid");

    const retry = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/retry.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(retry).toMatchObject({
      status: "completed",
      dbCommitted: true,
      roleChanged: false,
      revokedSessionCount: 1,
    });
    expect(fixture.users.find((row) => row.id === OPERATOR_ID)?.role).toBe(
      "admin",
    );
  });

  it("records acknowledged Redis progress and keeps inventory when a later batch acknowledgement is lost", async () => {
    const inventory = Array.from({ length: 60 }, (_, index) => ({
      token: `operator-session-${index}`,
      expiresAt: 4_102_444_800_000,
    }));
    const redis = new Map<string, string>([
      [`active-sessions-${OPERATOR_ID}`, JSON.stringify(inventory)],
      ...inventory.flatMap(({ token }) => [
        [token, "session-body"] as [string, string],
        [`bfx:mfa-verified:${token}`, "1"] as [string, string],
      ]),
    ]);
    const fixture = makeFixture({ redis, redisDeleteFailureCall: 2 });

    const first = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/batched-first.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(first).toMatchObject({
      status: "partial_failure",
      failureCode: "redis_outcome_unknown",
      dbCommitted: true,
      activeSessionCount: 60,
      revokedSessionCount: null,
      revokedKeyCount: 100,
      revokedKeyCountIsLowerBound: true,
      redisAcknowledgedBatchCount: 1,
      redisUnknownBatchNumber: 2,
      redisUnknownBatchKind: "session_keys",
      redisDeletionOutcome: "batch_acknowledgement_unknown",
      redisInventoryRetained: true,
      reconciliationRequired: true,
    });
    expect(redis.has(`active-sessions-${OPERATOR_ID}`)).toBe(true);

    const retry = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/batched-retry.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(retry).toMatchObject({
      status: "completed",
      roleChanged: false,
      revokedSessionCount: 60,
      revokedKeyCountIsLowerBound: false,
      redisDeletionOutcome: "completed",
      reconciliationRequired: false,
    });
    expect(redis.size).toBe(0);
  });

  it.each([
    ["existing path", "audit_exists"],
    ["unwritable path", "audit_reserve_failed"],
  ])("rejects %s before beginning a transaction", async (_, reserveError) => {
    const fixture = makeFixture({ reserveError });

    const result = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/existing.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({
      status: "failed",
      failureCode: reserveError,
    });
    expect(fixture.events).not.toContain("db:connect");
    expect(fixture.changedRoles()).toEqual([]);
  });

  it("returns partial failure when the reserved audit cannot be finalized", async () => {
    const fixture = makeFixture({ finalAuditError: "disk-secret-details" });

    const result = await main(
      [
        "--apply",
        "--confirm-bootstrap-admin",
        "--audit-file",
        "/audit/final-write.json",
      ],
      successfulEnv(),
      fixture.dependencies,
    );

    expect(result).toMatchObject({
      status: "partial_failure",
      failureCode: "audit_write_failed",
      dbCommitted: true,
    });
    expect(fixture.auditWrites).toHaveLength(1);
    expect(fixture.auditWrites[0]).toMatchObject({
      status: "reserved",
      dbCommitted: false,
    });
    expect(fixture.stderr.join("\n")).not.toContain("disk-secret-details");
  });
});
