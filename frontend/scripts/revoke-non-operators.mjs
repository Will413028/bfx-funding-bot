#!/usr/bin/env node

/**
 * Release 0 containment tool.
 *
 * The default is a read-only dry run. Applying the containment requires both
 * --apply and --confirm-release-0, validates the configured operator against
 * the auth database, bans every other auth user in one transaction, and then
 * removes that user's Better Auth secondary-storage sessions and MFA markers.
 * It is intentionally idempotent: a failed Redis cleanup can be retried with
 * the same command without unbanning or deleting any user data.
 */

import { randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import pg from "pg";
import Redis from "ioredis";

const { Pool } = pg;
const BAN_REASON = "release-0-non-operator-containment";
const MFA_MARKER_PREFIX = "bfx:mfa-verified:";
const SESSION_LIST_PREFIX = "active-sessions-";
const DELETE_BATCH_SIZE = 100;

class ContainmentError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = "ContainmentError";
    this.code = code;
  }
}

function usageError(message) {
  return new ContainmentError("invalid_arguments", message);
}

function parseArgs(argv) {
  let mode = "dry-run";
  let operatorId;
  let auditFile;
  let confirmed = false;

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") {
      return { help: true };
    }
    if (argument === "--dry-run") {
      if (mode === "apply") throw usageError("--dry-run and --apply are mutually exclusive");
      mode = "dry-run";
      continue;
    }
    if (argument === "--apply") {
      if (mode === "dry-run" && argv.includes("--dry-run")) {
        throw usageError("--dry-run and --apply are mutually exclusive");
      }
      mode = "apply";
      continue;
    }
    if (argument === "--confirm-release-0") {
      confirmed = true;
      continue;
    }
    if (argument === "--operator-id" || argument === "--audit-file") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw usageError(`${argument} requires a value`);
      }
      if (argument === "--operator-id") {
        if (operatorId !== undefined) throw usageError("--operator-id may only be specified once");
        operatorId = value.trim();
      } else {
        if (auditFile !== undefined) throw usageError("--audit-file may only be specified once");
        auditFile = value;
      }
      index += 1;
      continue;
    }
    if (argument.startsWith("--operator-id=")) {
      if (operatorId !== undefined) throw usageError("--operator-id may only be specified once");
      operatorId = argument.slice("--operator-id=".length).trim();
      continue;
    }
    if (argument.startsWith("--audit-file=")) {
      if (auditFile !== undefined) throw usageError("--audit-file may only be specified once");
      auditFile = argument.slice("--audit-file=".length);
      if (!auditFile) throw usageError("--audit-file requires a value");
      continue;
    }
    throw usageError(`unknown argument: ${argument}`);
  }

  if (mode === "apply" && !confirmed) {
    throw usageError("--apply requires --confirm-release-0");
  }
  if (mode === "dry-run" && confirmed) {
    throw usageError("--confirm-release-0 is only valid with --apply");
  }
  return { mode, operatorId, auditFile };
}

function requiredEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new ContainmentError("missing_configuration", `${name} is required`);
  return value;
}

function resolveOperatorId(cliOperatorId) {
  const envOperatorId = process.env.BFX_OPERATOR_USER_ID?.trim();
  if (cliOperatorId && envOperatorId && cliOperatorId !== envOperatorId) {
    throw new ContainmentError("operator_mismatch", "CLI and environment operator IDs differ");
  }
  const operatorId = cliOperatorId || envOperatorId;
  if (!operatorId) {
    throw new ContainmentError("missing_configuration", "BFX_OPERATOR_USER_ID or --operator-id is required");
  }
  if (process.env.BFX_OPERATOR_ROLE?.trim() !== "admin") {
    throw new ContainmentError("invalid_operator_role", "BFX_OPERATOR_ROLE must be admin");
  }
  return operatorId;
}

function validateOperator(row) {
  if (row.role !== "admin") {
    throw new ContainmentError("operator_role_mismatch", "configured operator row is not admin");
  }
  if (row.banned === true) {
    throw new ContainmentError("operator_banned", "configured operator is already banned");
  }
  if (row.twoFactorEnabled !== true) {
    throw new ContainmentError("operator_mfa_not_enrolled", "configured operator has no verified TOTP enrollment");
  }
}

function parseSessionList(raw, userId) {
  if (raw === null) return [];
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ContainmentError("invalid_session_inventory", `invalid session inventory for ${userId}`);
  }
  if (!Array.isArray(parsed)) {
    throw new ContainmentError("invalid_session_inventory", `invalid session inventory for ${userId}`);
  }
  const tokens = new Set();
  for (const entry of parsed) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.token !== "string" ||
      entry.token.length === 0
    ) {
      throw new ContainmentError("invalid_session_inventory", `invalid session entry for ${userId}`);
    }
    tokens.add(entry.token);
  }
  return [...tokens];
}

async function deleteInBatches(redis, keys) {
  let deletedKeyCount = 0;
  for (let index = 0; index < keys.length; index += DELETE_BATCH_SIZE) {
    const batch = keys.slice(index, index + DELETE_BATCH_SIZE);
    if (batch.length > 0) deletedKeyCount += await redis.del(...batch);
  }
  return deletedKeyCount;
}

async function updateUsers(pool, operatorId, mode) {
  const client = await pool.connect();
  let committed = false;
  try {
    await client.query("BEGIN");
    await client.query("SET LOCAL lock_timeout = '5s'");
    await client.query("SET LOCAL statement_timeout = '30s'");

    const operator = await client.query(
      `SELECT id, role, banned, "twoFactorEnabled"
         FROM auth."user"
        WHERE id = $1
        FOR UPDATE`,
      [operatorId],
    );
    if (operator.rowCount !== 1) {
      throw new ContainmentError("operator_not_found", "configured operator does not exist");
    }
    validateOperator(operator.rows[0]);

    if (mode === "dry-run") {
      const users = await client.query(
        `SELECT id FROM auth."user" WHERE id <> $1 ORDER BY id`,
        [operatorId],
      );
      await client.query("ROLLBACK");
      return { userIds: users.rows.map((row) => row.id), committed };
    }

    const users = await client.query(
      `UPDATE auth."user"
          SET banned = TRUE,
              "banReason" = $2,
              "banExpires" = NULL,
              "updatedAt" = CURRENT_TIMESTAMP
        WHERE id <> $1
        RETURNING id`,
      [operatorId, BAN_REASON],
    );
    await client.query("COMMIT");
    committed = true;
    return { userIds: users.rows.map((row) => row.id), committed };
  } catch (error) {
    if (!committed) {
      try {
        await client.query("ROLLBACK");
      } catch {
        // Preserve the original failure; the process remains fail-closed.
      }
    }
    if (error instanceof ContainmentError) throw error;
    throw new ContainmentError("database_operation_failed");
  } finally {
    client.release();
  }
}

async function inspectOrRevokeSessions(redis, userIds, mode) {
  const keysToDelete = new Set();
  let activeSessionCount = 0;

  for (const userId of userIds) {
    const listKey = `${SESSION_LIST_PREFIX}${userId}`;
    const tokens = parseSessionList(await redis.get(listKey), userId);
    activeSessionCount += tokens.length;
    if (mode === "apply") {
      keysToDelete.add(listKey);
      for (const token of tokens) {
        keysToDelete.add(token);
        keysToDelete.add(`${MFA_MARKER_PREFIX}${token}`);
      }
    }
  }

  const revokedKeyCount =
    mode === "apply" ? await deleteInBatches(redis, [...keysToDelete]) : 0;
  return { activeSessionCount, revokedKeyCount };
}

async function writeAudit(auditFile, record) {
  if (!auditFile) return;
  const path = resolve(auditFile);
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  await writeFile(path, `${JSON.stringify(record)}\n`, {
    encoding: "utf8",
    flag: "wx",
    mode: 0o600,
  });
}

function helpText() {
  return `Release 0 non-operator containment

Default: read-only dry run.

  node scripts/revoke-non-operators.mjs [--dry-run] [--operator-id ID] [--audit-file PATH]
  node scripts/revoke-non-operators.mjs --apply --confirm-release-0 [--operator-id ID] [--audit-file PATH]

Required environment: DATABASE_URL, REDIS_URL, BFX_OPERATOR_USER_ID, BFX_OPERATOR_ROLE=admin.
The configured operator must exist, be admin, not be banned, and have verified TOTP enrollment.`;
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : "invalid arguments");
    console.error(helpText());
    return 2;
  }
  if (options.help) {
    console.log(helpText());
    return 0;
  }

  const operation = {
    operationId: randomUUID(),
    startedAt: new Date().toISOString(),
    mode: options.mode,
    operatorUserId: options.operatorId ?? process.env.BFX_OPERATOR_USER_ID?.trim() ?? null,
    status: "failed",
    dbCommitted: false,
    nonOperatorCount: 0,
    nonOperatorUserIds: [],
    activeSessionCount: 0,
    revokedKeyCount: 0,
  };
  let pool;
  let redis;

  try {
    const databaseUrl = requiredEnv("DATABASE_URL");
    const redisUrl = requiredEnv("REDIS_URL");
    const operatorId = resolveOperatorId(options.operatorId);
    operation.operatorUserId = operatorId;

    pool = new Pool({
      connectionString: databaseUrl,
      max: 1,
      connectionTimeoutMillis: 10_000,
    });
    const update = await updateUsers(pool, operatorId, options.mode);
    operation.dbCommitted = update.committed;
    operation.nonOperatorUserIds = update.userIds;
    operation.nonOperatorCount = update.userIds.length;

    redis = new Redis(redisUrl, {
      lazyConnect: true,
      maxRetriesPerRequest: 1,
      enableOfflineQueue: false,
    });
    await redis.connect();
    const sessions = await inspectOrRevokeSessions(
      redis,
      update.userIds,
      options.mode,
    );
    operation.activeSessionCount = sessions.activeSessionCount;
    operation.revokedKeyCount = sessions.revokedKeyCount;
    operation.status = "completed";
  } catch (error) {
    operation.status = operation.dbCommitted ? "partial_failure" : "failed";
    operation.failureCode =
      error instanceof ContainmentError ? error.code : "dependency_unavailable";
    try {
      await writeAudit(options.auditFile, {
        ...operation,
        finishedAt: new Date().toISOString(),
      });
    } catch {
      console.error("operator containment audit could not be written");
    }
    console.error(`operator containment ${operation.status}: ${operation.failureCode}`);
    return 1;
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

  operation.finishedAt = new Date().toISOString();
  try {
    await writeAudit(options.auditFile, operation);
  } catch {
    console.error("operator containment completed but audit could not be written");
    return 1;
  }
  console.log(JSON.stringify(operation));
  return 0;
}

const exitCode = await main();
process.exitCode = exitCode;
