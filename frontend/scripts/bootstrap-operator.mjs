#!/usr/bin/env node

/**
 * Audited first-admin bootstrap.
 *
 * This host-only command assigns the configured Better Auth user its first
 * exact `admin` role only after the production auth schema proves a credential
 * account and one verified TOTP enrollment. It never creates or repairs auth
 * state. Apply revokes only the configured operator's inventoried sessions so
 * the human must sign in and complete MFA again.
 */

import { randomUUID } from "node:crypto";
import { lstat, open, rename, unlink } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import Redis from "ioredis";
import pg from "pg";

const { Pool } = pg;
const SESSION_LIST_PREFIX = "active-sessions-";
const MFA_MARKER_PREFIX = "bfx:mfa-verified:";
const DELETE_BATCH_SIZE = 100;
const MAX_SESSION_INVENTORY_BYTES = 1024 * 1024;
const MAX_SESSION_COUNT = 1000;
const MAX_SESSION_TOKEN_BYTES = 1024;

export class BootstrapError extends Error {
  constructor(code) {
    super(code);
    this.name = "BootstrapError";
    this.code = code;
  }
}

function fail(code) {
  throw new BootstrapError(code);
}

function parseArgs(argv) {
  let mode = "dry-run";
  let explicitDryRun = false;
  let confirmed = false;
  let operatorId;
  let auditFile;

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") return { help: true };
    if (argument === "--dry-run") {
      if (mode === "apply" || explicitDryRun) fail("invalid_arguments");
      explicitDryRun = true;
      continue;
    }
    if (argument === "--apply") {
      if (mode === "apply" || explicitDryRun) fail("invalid_arguments");
      mode = "apply";
      continue;
    }
    if (argument === "--confirm-bootstrap-admin") {
      if (confirmed) fail("invalid_arguments");
      confirmed = true;
      continue;
    }
    if (argument === "--operator-id" || argument === "--audit-file") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) fail("invalid_arguments");
      if (argument === "--operator-id") {
        if (operatorId !== undefined) fail("invalid_arguments");
        operatorId = value.trim();
        if (!operatorId) fail("invalid_arguments");
      } else {
        if (auditFile !== undefined) fail("invalid_arguments");
        auditFile = value;
      }
      index += 1;
      continue;
    }
    if (argument.startsWith("--operator-id=")) {
      if (operatorId !== undefined) fail("invalid_arguments");
      operatorId = argument.slice("--operator-id=".length).trim();
      if (!operatorId) fail("invalid_arguments");
      continue;
    }
    if (argument.startsWith("--audit-file=")) {
      if (auditFile !== undefined) fail("invalid_arguments");
      auditFile = argument.slice("--audit-file=".length);
      if (!auditFile) fail("invalid_arguments");
      continue;
    }
    fail("invalid_arguments");
  }

  if (mode === "apply" && (!confirmed || !auditFile)) {
    fail("invalid_arguments");
  }
  if (mode === "dry-run" && confirmed) fail("invalid_arguments");
  return { mode, operatorId, auditFile };
}

function requiredEnv(env, name) {
  const value = env[name]?.trim();
  if (!value) fail("missing_configuration");
  return value;
}

function configuredOperatorId(env, cliOperatorId) {
  const configured = requiredEnv(env, "BFX_OPERATOR_USER_ID");
  if (cliOperatorId && cliOperatorId !== configured) fail("operator_mismatch");
  if (requiredEnv(env, "BFX_OPERATOR_ROLE") !== "admin") {
    fail("invalid_operator_role");
  }
  return configured;
}

function roleContainsAdmin(role) {
  return (
    typeof role === "string" &&
    role
      .split(",")
      .map((part) => part.trim())
      .includes("admin")
  );
}

function validateUsers(users, operatorId) {
  const targets = users.filter((row) => row.id === operatorId);
  if (targets.length === 0) fail("operator_not_found");
  if (targets.length !== 1) fail("operator_ambiguous");
  const operator = targets[0];
  if (operator.banned === true) fail("operator_banned");
  if (operator.twoFactorEnabled !== true) fail("operator_mfa_not_enabled");

  for (const row of users) {
    if (!roleContainsAdmin(row.role)) continue;
    if (row.id !== operatorId || row.role !== "admin") fail("admin_conflict");
  }
  return operator;
}

function validateCredential(rows, operatorId) {
  if (rows.length === 0) fail("credential_not_found");
  if (rows.length !== 1) fail("credential_ambiguous");
  const credential = rows[0];
  if (
    credential.userId !== operatorId ||
    credential.accountId !== operatorId ||
    credential.providerId !== "credential" ||
    credential.hasPassword !== true
  ) {
    fail("credential_invalid");
  }
}

function validateTotp(rows, operatorId) {
  if (rows.length > 1) fail("verified_totp_ambiguous");
  if (
    rows.length !== 1 ||
    rows[0].userId !== operatorId ||
    rows[0].verified !== true
  ) {
    fail("verified_totp_not_found");
  }
}

async function inspectAndAssign(pool, operatorId, mode) {
  const client = await pool.connect();
  let transactionOpen = false;
  let committed = false;
  try {
    await client.query(mode === "dry-run" ? "BEGIN READ ONLY" : "BEGIN");
    transactionOpen = true;
    await client.query("SET LOCAL lock_timeout = '5s'");
    await client.query("SET LOCAL statement_timeout = '30s'");
    if (mode === "apply") {
      await client.query(
        'LOCK TABLE auth."user" IN SHARE ROW EXCLUSIVE MODE',
      );
    }

    const usersResult = await client.query(
      'SELECT id, role, banned, "twoFactorEnabled" FROM auth."user" ORDER BY id',
    );
    const operator = validateUsers(usersResult.rows, operatorId);
    const credentialResult = await client.query(
      `SELECT id, "userId", "accountId", "providerId",
              ("password" IS NOT NULL AND "password" <> '') AS "hasPassword"
         FROM auth."account"
        WHERE "userId" = $1 AND "providerId" = 'credential'
        ORDER BY id`,
      [operatorId],
    );
    validateCredential(credentialResult.rows, operatorId);
    const totpResult = await client.query(
      `SELECT id, "userId", verified
         FROM auth."twoFactor"
        WHERE "userId" = $1
        ORDER BY id`,
      [operatorId],
    );
    validateTotp(totpResult.rows, operatorId);

    if (mode === "dry-run") {
      await client.query("ROLLBACK");
      transactionOpen = false;
      return { dbCommitted: false, roleChanged: false };
    }

    let roleChanged = false;
    if (operator.role !== "admin") {
      const update = await client.query(
        `UPDATE auth."user"
            SET role = 'admin', "updatedAt" = CURRENT_TIMESTAMP
          WHERE id = $1`,
        [operatorId],
      );
      if (update.rowCount !== 1) fail("operator_update_failed");
      roleChanged = true;
    }
    await client.query("COMMIT");
    transactionOpen = false;
    committed = true;
    return { dbCommitted: true, roleChanged };
  } catch (error) {
    if (transactionOpen && !committed) {
      try {
        await client.query("ROLLBACK");
      } catch {
        // Preserve the bounded validation failure; no mutation was committed.
      }
    }
    if (error instanceof BootstrapError) throw error;
    fail("database_operation_failed");
  } finally {
    client.release();
  }
}

function parseSessionInventory(raw) {
  if (raw === null) return [];
  if (Buffer.byteLength(raw, "utf8") > MAX_SESSION_INVENTORY_BYTES) {
    fail("invalid_session_inventory");
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    fail("invalid_session_inventory");
  }
  if (!Array.isArray(parsed) || parsed.length > MAX_SESSION_COUNT) {
    fail("invalid_session_inventory");
  }
  const tokens = new Set();
  for (const entry of parsed) {
    if (
      entry === null ||
      typeof entry !== "object" ||
      typeof entry.token !== "string" ||
      entry.token.length === 0 ||
      Buffer.byteLength(entry.token, "utf8") > MAX_SESSION_TOKEN_BYTES ||
      entry.token.includes("\0") ||
      typeof entry.expiresAt !== "number" ||
      !Number.isFinite(entry.expiresAt) ||
      entry.expiresAt <= 0
    ) {
      fail("invalid_session_inventory");
    }
    tokens.add(entry.token);
  }
  return [...tokens];
}

async function inspectOrRevokeSessions(redis, operatorId, mode) {
  const listKey = `${SESSION_LIST_PREFIX}${operatorId}`;
  let tokens;
  try {
    tokens = parseSessionInventory(await redis.get(listKey));
  } catch (error) {
    if (error instanceof BootstrapError) throw error;
    fail("redis_operation_failed");
  }

  if (mode === "dry-run") {
    return {
      activeSessionCount: tokens.length,
      revokedSessionCount: 0,
      revokedKeyCount: 0,
    };
  }

  const keys = [
    ...tokens.flatMap((token) => [token, `${MFA_MARKER_PREFIX}${token}`]),
    // Keep the only bounded inventory until every token/marker batch succeeds.
    // A retry can then resolve and safely re-delete keys from earlier batches.
    listKey,
  ];
  let revokedKeyCount = 0;
  try {
    for (let index = 0; index < keys.length; index += DELETE_BATCH_SIZE) {
      revokedKeyCount += await redis.del(
        ...keys.slice(index, index + DELETE_BATCH_SIZE),
      );
    }
  } catch {
    fail("redis_operation_failed");
  }
  return {
    activeSessionCount: tokens.length,
    revokedSessionCount: tokens.length,
    revokedKeyCount,
  };
}

async function reserveAuditFile(path, initial) {
  const auditPath = resolve(path);
  let handle;
  let reservedStat;
  try {
    handle = await open(auditPath, "wx", 0o600);
  } catch (error) {
    if (error?.code === "EEXIST") fail("audit_exists");
    fail("audit_reserve_failed");
  }
  try {
    await handle.chmod(0o600);
    reservedStat = await handle.stat();
    if (
      !reservedStat.isFile() ||
      (reservedStat.mode & 0o777) !== 0o600 ||
      reservedStat.nlink !== 1
    ) {
      fail("audit_reserve_failed");
    }
    await writeAuditRecord(handle, initial);
    await handle.sync();
    await syncDirectory(dirname(auditPath));
  } catch (error) {
    await handle.close().catch(() => {});
    if (error instanceof BootstrapError) throw error;
    fail("audit_reserve_failed");
  }
  return {
    async write(record) {
      const temporaryPath = `${auditPath}.${randomUUID()}.tmp`;
      let temporaryHandle;
      try {
        temporaryHandle = await open(temporaryPath, "wx", 0o600);
        await temporaryHandle.chmod(0o600);
        const temporaryStat = await temporaryHandle.stat();
        if (
          !temporaryStat.isFile() ||
          (temporaryStat.mode & 0o777) !== 0o600 ||
          temporaryStat.nlink !== 1
        ) {
          fail("audit_write_failed");
        }
        await writeAuditRecord(temporaryHandle, record);
        await temporaryHandle.sync();
        await temporaryHandle.close();
        temporaryHandle = undefined;

        const currentStat = await lstat(auditPath);
        if (
          !currentStat.isFile() ||
          currentStat.dev !== reservedStat.dev ||
          currentStat.ino !== reservedStat.ino ||
          (currentStat.mode & 0o777) !== 0o600 ||
          currentStat.nlink !== 1
        ) {
          fail("audit_write_failed");
        }
        await handle.close();
        handle = undefined;
        await rename(temporaryPath, auditPath);
        await syncDirectory(dirname(auditPath));
      } catch {
        if (temporaryHandle) await temporaryHandle.close().catch(() => {});
        await unlink(temporaryPath).catch(() => {});
        fail("audit_write_failed");
      }
    },
    async close() {
      if (handle) await handle.close();
    },
  };
}

async function writeAuditRecord(handle, record) {
  const content = Buffer.from(`${JSON.stringify(record)}\n`, "utf8");
  let offset = 0;
  while (offset < content.length) {
    const { bytesWritten } = await handle.write(
      content,
      offset,
      content.length - offset,
      offset,
    );
    if (bytesWritten <= 0) fail("audit_write_failed");
    offset += bytesWritten;
  }
}

async function syncDirectory(path) {
  const directory = await open(path, "r");
  try {
    await directory.sync();
  } finally {
    await directory.close();
  }
}

const defaultDependencies = {
  randomUUID,
  now: () => new Date(),
  createPool: (databaseUrl) =>
    new Pool({
      connectionString: databaseUrl,
      max: 1,
      connectionTimeoutMillis: 10_000,
    }),
  createRedis: (redisUrl) =>
    new Redis(redisUrl, {
      lazyConnect: true,
      maxRetriesPerRequest: 1,
      enableOfflineQueue: false,
      connectTimeout: 10_000,
      commandTimeout: 10_000,
    }),
  reserveAudit: reserveAuditFile,
  stdout: (line) => console.log(line),
  stderr: (line) => console.error(line),
};

function helpText() {
  return `Audited first-admin bootstrap

Default: read-only dry run.

  node scripts/bootstrap-operator.mjs [--dry-run] [--operator-id ID] [--audit-file PATH]
  node scripts/bootstrap-operator.mjs --apply --confirm-bootstrap-admin --audit-file PATH [--operator-id ID]

Required environment: DATABASE_URL, REDIS_URL, BFX_OPERATOR_USER_ID, BFX_OPERATOR_ROLE=admin.`;
}

function initialRecord(dependencies, mode, operatorUserId) {
  return {
    schemaVersion: 1,
    operation: "bootstrap_operator",
    operationId: dependencies.randomUUID(),
    startedAt: dependencies.now().toISOString(),
    mode,
    operatorUserId,
    status: "reserved",
    dbCommitted: false,
    roleChanged: false,
    activeSessionCount: 0,
    revokedSessionCount: 0,
    revokedKeyCount: 0,
  };
}

function failureCode(error, fallback) {
  return error instanceof BootstrapError ? error.code : fallback;
}

/**
 * @param {string[]} [argv]
 * @param {Record<string, string | undefined>} [env]
 * @param {any} [dependencies]
 */
export async function main(argv = [], env = process.env, dependencies = defaultDependencies) {
  let options;
  let operation = initialRecord(
    dependencies,
    "dry-run",
    env.BFX_OPERATOR_USER_ID?.trim() || null,
  );
  let audit;
  let pool;
  let redis;

  try {
    options = parseArgs(argv);
    if (options.help) {
      dependencies.stdout(helpText());
      return { ...operation, status: "help" };
    }
    operation.mode = options.mode;
    const databaseUrl = requiredEnv(env, "DATABASE_URL");
    const redisUrl = requiredEnv(env, "REDIS_URL");
    const operatorId = configuredOperatorId(env, options.operatorId);
    operation.operatorUserId = operatorId;

    if (options.auditFile) {
      try {
        audit = await dependencies.reserveAudit(options.auditFile, operation);
      } catch (error) {
        throw new BootstrapError(
          error instanceof BootstrapError ||
            error?.message === "audit_exists" ||
            error?.message === "audit_reserve_failed"
            ? error.code ?? error.message
            : "audit_reserve_failed",
        );
      }
    }

    pool = dependencies.createPool(databaseUrl);
    const database = await inspectAndAssign(pool, operatorId, options.mode);
    operation.dbCommitted = database.dbCommitted;
    operation.roleChanged = database.roleChanged;

    redis = dependencies.createRedis(redisUrl);
    try {
      await redis.connect();
    } catch {
      fail("redis_operation_failed");
    }
    const sessions = await inspectOrRevokeSessions(redis, operatorId, options.mode);
    operation = { ...operation, ...sessions, status: "completed" };
  } catch (error) {
    operation.status = operation.dbCommitted ? "partial_failure" : "failed";
    operation.failureCode = failureCode(error, "dependency_unavailable");
  } finally {
    if (redis) {
      try {
        await redis.quit();
      } catch {
        redis.disconnect();
      }
    }
    if (pool) await pool.end();
  }

  operation.finishedAt = dependencies.now().toISOString();
  if (audit) {
    try {
      await audit.write(operation);
    } catch {
      operation.status = operation.dbCommitted ? "partial_failure" : "failed";
      operation.failureCode = "audit_write_failed";
    } finally {
      try {
        await audit.close();
      } catch {
        operation.status = operation.dbCommitted ? "partial_failure" : "failed";
        operation.failureCode = "audit_write_failed";
      }
    }
  }

  if (operation.status === "completed") {
    dependencies.stdout(JSON.stringify(operation));
  } else {
    dependencies.stderr(
      `operator bootstrap ${operation.status}: ${operation.failureCode}`,
    );
  }
  return operation;
}

function isDirectExecution() {
  if (!process.argv[1]) return false;
  return pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
}

if (isDirectExecution()) {
  main(process.argv.slice(2), process.env).then((operation) => {
    process.exitCode = operation.status === "completed" || operation.status === "help" ? 0 : 1;
  });
}
