#!/usr/bin/env python3
"""Run the weekly measurement chain on the deployed backend, from the deployed tooling.

Runs as root from bfx-weekly-report.service (Mon 04:17 UTC; by hand:
`sudo systemctl start bfx-weekly-report.service`), with the interpreter of the tooling release it lives in
(/usr/local/lib/bfx-ops/releases/<rev>/ops, reached through `current`). Nothing
here reads the VM mirror's working tree: the job's steps are
docker-compose.weekly-report.yml next to this file, installed by bfx-deploy after
each successful deploy together with the rest of the host tooling.

One run:
  1. the steps: this release's docker-compose.weekly-report.yml (resolved once, so
     a deploy that moves `current` mid-run does not change what runs);
  2. the image: the ledger's newest `deployed` row, as
     `<backend repository>@<backend digest>` -- the backend production runs, never
     the mutable bfx-bot:local tag. Its revision's live.env (materialized by
     bfx-deploy under /var/lib/bfx-deploy/releases/<rev>) supplies
     BFX_DEPLOYMENT_ENV. When the tooling release differs from the deployed one
     (a tooling install failed after that deploy) the run goes ahead with the
     installed steps and says so;
  3. the secrets: DATABASE_URL and BFX_EXCHANGE_ACCOUNT_ID only, from
     /opt/bfx/runtime/bot.env (root 0600, checked as bfx-deploy checks it);
  4. `docker compose -p bfx-weekly-report -f <that file> run --rm weekly-report`,
     output streamed to the journal. systemd's TimeoutStartSec bounds the run.

`--dry-run` prints the resolved plan (no secret values) and runs nothing.
Container hardening is a property of the compose file and is enforced in CI
(compose_policy.py --kind weekly-report).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx_deploy = _load("_bfx_ops_deploy", _HERE / "bfx_deploy.py")
DeployError = bfx_deploy.DeployError

PROJECT = "bfx-weekly-report"
SERVICE = "weekly-report"
COMPOSE_FILE = "docker-compose.weekly-report.yml"
# The only runtime secrets the steps read (scripts/run_weekly_attribution.py,
# scripts/report_interest.py, Settings.database_url); nothing else of bot.env.
SECRET_KEYS = ("DATABASE_URL", "BFX_EXCHANGE_ACCOUNT_ID")
LIVE_KEYS = ("BFX_DEPLOYMENT_ENV",)


def log(message: str) -> None:
    print(f"bfx-weekly-report: {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class Plan:
    compose_file: Path
    tooling_revision: str
    deployed_revision: str
    backend_digest: str
    backend_image: str
    env: Mapping[str, str]  # holds secrets: never logged or printed

    @property
    def argv(self) -> list[str]:
        return ["docker", "compose", "-p", PROJECT, "-f", str(self.compose_file),
                "run", "--rm", SERVICE]

    def describe(self) -> dict[str, object]:
        return {"compose_file": str(self.compose_file), "tooling_revision": self.tooling_revision,
                "deployed_revision": self.deployed_revision, "backend_image": self.backend_image,
                "steps_match_deployed_release": self.tooling_revision == self.deployed_revision,
                "env_keys": sorted(k for k in self.env if k in (*SECRET_KEYS, *LIVE_KEYS)),
                "argv": self.argv}


def tooling_revision(ops_dir: Path) -> str:
    """The release this file was installed from (bfx-deploy writes `.installed`)."""
    try:
        revision = (ops_dir.parent / ".installed").read_text(encoding="utf-8").strip()
    except OSError:
        raise DeployError("tooling_release_unknown") from None
    if bfx_deploy.REVISION.fullmatch(revision) is None:
        raise DeployError("tooling_release_unknown")
    return revision


def _values(path: Path, keys: Sequence[str], *, secret_check: Callable[[Path], None] | None) -> dict[str, str]:
    if secret_check is not None:
        secret_check(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise DeployError(f"env_file_unreadable:{path.name}") from None
    values = bfx_deploy.parse_env_file(text, name=path.name, compose=False)
    missing = [key for key in keys if not values.get(key, "").strip()]
    if missing:
        raise DeployError(f"env_key_missing:{path.name}:{','.join(missing)}")
    return {key: values[key] for key in keys}


def build_plan(
    *, ops_dir: Path, ledger: Any, runtime_dir: Path, state_dir: Path,
    backend_repository: str, secret_check: Callable[[Path], None],
) -> Plan:
    compose_file = ops_dir / COMPOSE_FILE
    if not compose_file.is_file():
        raise DeployError("weekly_compose_missing")
    revision = tooling_revision(ops_dir)
    view = ledger.read()
    deployed = view.last_success if view.exists else None
    if deployed is None:
        raise DeployError("no_deployed_release")
    digest = deployed.backend_digest
    if bfx_deploy.DIGEST.fullmatch(digest) is None \
            or bfx_deploy.REVISION.fullmatch(deployed.source_revision) is None:
        raise DeployError("ledger_row_invalid")
    image = f"{backend_repository}@{digest}"
    live_env = state_dir / "releases" / deployed.source_revision / "live.env"
    env = {
        **bfx_deploy._base_env(),
        **_values(runtime_dir / "bot.env", SECRET_KEYS, secret_check=secret_check),
        **_values(live_env, LIVE_KEYS, secret_check=None),
        "BFX_BACKEND_IMAGE": image,
        "BFX_BACKEND_DIGEST": digest,
        "BFX_SOURCE_REVISION": deployed.source_revision,
    }
    return Plan(compose_file=compose_file, tooling_revision=revision,
                deployed_revision=deployed.source_revision, backend_digest=digest,
                backend_image=image, env=env)


def run(plan: Plan, *, execute: Callable[..., int] | None = None) -> int:
    if plan.tooling_revision != plan.deployed_revision:
        log(f"warning: steps from tooling release {plan.tooling_revision[:12]}, image from "
            f"deployed release {plan.deployed_revision[:12]} (tooling install failed after "
            "that deploy?); running anyway")
    log(f"steps {plan.tooling_revision[:12]}, image {plan.backend_image}")
    if execute is None:
        def execute(argv: list[str], env: Mapping[str, str]) -> int:
            return subprocess.run(argv, env=dict(env), check=False).returncode
    returncode = execute(plan.argv, plan.env)
    log(f"finished with exit code {returncode}")
    return returncode


def _parser() -> argparse.ArgumentParser:
    defaults = bfx_deploy.Settings()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime-dir", type=Path, default=defaults.runtime_dir)
    parser.add_argument("--state-dir", type=Path, default=defaults.state_dir)
    parser.add_argument("--backend-repository", default=defaults.backend_repository)
    parser.add_argument("--postgres-container", default=defaults.postgres_container)
    parser.add_argument("--db-user", default=defaults.db_user)
    parser.add_argument("--db-name", default=defaults.db_name)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved plan (no secret values); run nothing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        log("must run as root (reads /opt/bfx/runtime and the deployments ledger)")
        return 2
    ledger = bfx_deploy.PsqlLedger(bfx_deploy.subprocess_runner, container=args.postgres_container,
                                   db_user=args.db_user, db_name=args.db_name)
    try:
        plan = build_plan(ops_dir=_HERE, ledger=ledger, runtime_dir=args.runtime_dir,
                          state_dir=args.state_dir, backend_repository=args.backend_repository,
                          secret_check=bfx_deploy.protected_secret_file)
    except DeployError as exc:
        log(f"cannot run: {exc.code}")
        return 1
    if args.dry_run:
        print(json.dumps(plan.describe(), indent=2))
        return 0
    return run(plan)


if __name__ == "__main__":
    raise SystemExit(main())
