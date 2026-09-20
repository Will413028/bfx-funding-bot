import { EventEmitter } from "node:events";
import Redis from "ioredis";
import { Pool } from "pg";
import { main } from "../../../../scripts/bootstrap-operator.mjs";

// Synthetic wire client, real pg.Pool acquisition/release/idle listener.
const scenario = process.argv[2];
const receipts = [];
let committed = false;
let poolEvents = 0;
let closedBeforeRedis = false;
let client;
class SyntheticClient extends EventEmitter {
  _queryable = true;
  connect(callback) { client = this; callback(); }
  end(callback) { callback?.(); }
  async query(sql) {
    if (sql.includes('FROM auth."user"')) return { rows: [{ id: "operator", role: "user", banned: false, twoFactorEnabled: true }] };
    if (sql.includes('FROM auth."account"')) return { rows: [{ userId: "operator", accountId: "operator", providerId: "credential", hasPassword: true }] };
    if (sql.includes('FROM auth."twoFactor"')) return { rows: [{ userId: "operator", verified: true }] };
    if (sql === "COMMIT") committed = true;
    return { rows: [], rowCount: 1 };
  }
}
const pool = new Pool({ Client: SyntheticClient, idleTimeoutMillis: 0 });
// Do not register a test error listener: that would mask the missing handler.
const emit = pool.emit;
pool.emit = function (name, ...args) {
  if (name === "error") poolEvents++;
  return emit.call(this, name, ...args);
};
pool.on("release", () => {
  if (scenario === "release") queueMicrotask(() => client.emit("error", new Error("synthetic-private-disconnect")));
});
const result = await main(
  ["--apply", "--confirm-bootstrap-admin", "--audit-file", "synthetic-memory-audit"],
  { DATABASE_URL: "postgresql://fixture.invalid/auth", REDIS_URL: "redis://fixture.invalid", BFX_OPERATOR_USER_ID: "operator", BFX_OPERATOR_ROLE: "admin" },
  {
    randomUUID: () => "synthetic-operation",
    now: () => new Date("2026-09-20T00:00:00Z"),
    createPool: () => pool,
    // Real Redis silentEmit; only network/commands are replaced. lazyConnect
    // ensures constructing this fixture never opens a socket.
    createRedis: () => Object.assign(new Redis({ lazyConnect: true }), {
      async connect() {
        closedBeforeRedis = pool.ended;
        if (scenario === "redis-error-connect") {
          const error = new Error("synthetic-private-redis-connect");
          this.silentEmit("error", error);
          throw error;
        }
        if (scenario === "redis") await new Promise((resolve) => setImmediate(() => {
          if (pool.idleCount) client.emit("error", new Error("synthetic-private-disconnect"));
          resolve();
        }));
      },
      async get() { return JSON.stringify([{ token: "synthetic-session", expiresAt: 4102444800000 }]); },
      async del(...keys) { return keys.length; },
      async quit() {
        if (scenario === "redis-error-late") this.silentEmit("error", new Error("synthetic-private-redis-late"));
      },
      disconnect() {},
    }),
    reserveAudit: async (_path, initial) => {
      receipts.push(structuredClone(initial));
      return { async write(record) { receipts.push(structuredClone(record)); }, async close() {} };
    },
    stdout() {},
    stderr: (line) => process.stderr.write(`${line}\n`),
  },
);
console.log(JSON.stringify({ result, receipts, committed, poolEvents, closedBeforeRedis }));
process.exitCode = result.status === "completed" ? 0 : 1;
