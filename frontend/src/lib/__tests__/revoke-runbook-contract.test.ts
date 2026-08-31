import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const frontendRoot = join(__dirname, "../../..");

describe("Release 0 operator containment runbook", () => {
  it("ships a fail-closed, auditable non-operator revocation tool", () => {
    const toolPath = join(frontendRoot, "scripts/revoke-non-operators.mjs");
    const dockerfile = readFileSync(join(frontendRoot, "Dockerfile"), "utf8");
    const runbook = readFileSync(
      join(frontendRoot, "../docs/runbooks/release-0-operator-containment.md"),
      "utf8",
    );

    expect(existsSync(toolPath)).toBe(true);
    const source = readFileSync(toolPath, "utf8");

    expect(source).toContain("--dry-run");
    expect(source).toContain("--apply");
    expect(source).toContain("--confirm-release-0");
    expect(source).toContain('UPDATE auth."user"');
    expect(source).toContain("active-sessions-");
    expect(source).toContain("bfx:mfa-verified:");
    expect(source).toContain("FOR UPDATE");
    expect(source).toContain("audit-file");
    expect(source).not.toMatch(/FLUSH(?:ALL|DB)/i);
    expect(dockerfile).toContain("/app/scripts ./scripts");
    expect(runbook).toContain(
      "docker compose --env-file .env.frontend.runtime -f docker-compose.bot.yml run",
    );
    expect(runbook).toContain("--env-file .env.frontend.runtime");
    expect(runbook).toContain('--user "$(id -u):$(id -g)"');
  });
});
