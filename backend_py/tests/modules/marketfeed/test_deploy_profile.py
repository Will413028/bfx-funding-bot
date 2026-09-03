"""Deployment profiles keep execution policies explicit and fail-closed."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bfx_funding_bot.modules.marketfeed.config import load_cells_only

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def parse_env_file(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text().splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#") and "=" in stripped
        for key, value in [stripped.split("=", maxsplit=1)]
    }


def test_shadow_p14_is_shadow_only() -> None:
    env = parse_env_file(REPOSITORY_ROOT / "deploy/vm/shadow-p14.env")

    assert env["BFX_PHASE"] == "shadow"
    assert env["BFX_DEPLOYMENT_ENV"] == "shadow"
    assert env["BFX_CELLS_YAML"].endswith("cells.experimental-p14.yaml")
    assert env["BFX_EXECUTION_POLICY"] == "optimizer_shadow"


def test_deployment_profiles_contain_no_legacy_clamp_flags() -> None:
    for path in (REPOSITORY_ROOT / "deploy/vm").glob("*.env"):
        assert "BFX_CLAMP_" not in path.read_text()


@pytest.mark.parametrize("legacy_var", ["BFX_ACCOUNT_ID", "BFX_API_KEY", "BFX_API_SECRET"])
def test_deploy_script_rejects_legacy_identity_and_env_credentials(
    tmp_path: Path, legacy_var: str
) -> None:
    root = _deploy_root(tmp_path)
    bot_env = root.parent / "home/bfx/bot.env"
    bot_env.write_text(bot_env.read_text() + f"{legacy_var}=legacy\n")

    result = _run_deploy(root, "paper")

    assert result.returncode != 0
    assert legacy_var in result.stdout
    assert not (root / "fake-docker.log").exists()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _deploy_root(tmp_path: Path) -> Path:
    root = tmp_path / "vm-repo"
    (root / "scripts").mkdir(parents=True)
    (root / "deploy/vm").mkdir(parents=True)
    (root / "backend_py/configs").mkdir(parents=True)
    (root / "scripts/deploy-vm.sh").symlink_to(
        REPOSITORY_ROOT / "scripts/deploy-vm.sh"
    )
    for profile in ("paper.env", "shadow.env", "shadow-p14.env", "canary.env"):
        shutil.copy2(REPOSITORY_ROOT / "deploy/vm" / profile, root / "deploy/vm" / profile)
    evidence = tmp_path / "halt2-canary-evidence.json"
    evidence.write_text("{}\n")
    canary_profile = root / "deploy/vm/canary.env"
    canary_profile.write_text(
        canary_profile.read_text().replace(
            "/var/lib/bfx/evidence/halt2-canary.json", str(evidence)
        )
    )
    for config in ("cells.experimental-p14.yaml", "cells.canary.yaml", "safety.canary.yaml"):
        shutil.copy2(
            REPOSITORY_ROOT / "backend_py/configs" / config,
            root / "backend_py/configs" / config,
        )

    home = tmp_path / "home/bfx"
    home.mkdir(parents=True)
    (home / "bot.env").write_text(
        "DATABASE_URL=postgresql://safe-fake\n"
        "BFX_EXCHANGE_ACCOUNT_ID=550e8400-e29b-41d4-a716-446655440000\n"
        "BFX_VAULT_KEK=safe-fake\n"
    )
    (home / "webapi.env").write_text(
        "DATABASE_URL=postgresql://safe-fake\n"
        "BETTER_AUTH_JWKS_URL=https://example.invalid/jwks\n"
        "BFX_VAULT_KEK=safe-fake\n"
        "BFX_OPERATOR_USER_ID=operator-1\n"
        "BFX_OPERATOR_ROLE=admin\n"
    )
    (home / "frontend.env").write_text(
        "NEXT_PUBLIC_APP_URL=https://example.invalid\n"
        "NEXT_PUBLIC_BETTER_AUTH_URL=https://example.invalid/auth\n"
        "API_URL=https://example.invalid/api\n"
        "BETTER_AUTH_SECRET=safe-fake\n"
        "BETTER_AUTH_URL=https://example.invalid/auth\n"
        "DATABASE_URL=postgresql://safe-fake\n"
        "REDIS_URL=redis://safe-fake\n"
        "PASSKEY_RP_ID=example.invalid\n"
        "BFX_OPERATOR_USER_ID=operator-1\n"
        "BFX_OPERATOR_ROLE=admin\n"
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "git",
        "#!/bin/sh\n"
        "if [ \"$1\" = pull ]; then exit 0; fi\n"
        "if [ \"$1\" = rev-parse ]; then printf '%s\\n' safe-test-sha; exit 0; fi\n"
        "exit 1\n",
    )
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_LOG\"\n",
    )
    _write_executable(
        fake_bin / "uv",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_UV_LOG\"\nexit \"${FAKE_UV_EXIT:-0}\"\n",
    )
    return root


def _run_deploy(root: Path, phase: str, *, confirm: bool = False) -> subprocess.CompletedProcess[str]:
    fake_bin = root.parent / "fake-bin"
    environment = os.environ | {
        "HOME": str(root.parent / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(root / "fake-docker.log"),
        "FAKE_UV_LOG": str(root / "fake-uv.log"),
    }
    if confirm:
        environment["BFX_CANARY_CONFIRM"] = "yes"
    else:
        environment.pop("BFX_CANARY_CONFIRM", None)
    return subprocess.run(
        [str(root / "scripts/deploy-vm.sh"), phase],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("phase", "wrong_policy"),
    [
        ("paper", "book_guarded"),
        ("shadow", "paper"),
        ("shadow-p14", "book_guarded"),
        ("canary", "paper"),
    ],
)
def test_deploy_script_rejects_phase_policy_mismatch_before_docker(
    tmp_path: Path, phase: str, wrong_policy: str
) -> None:
    root = _deploy_root(tmp_path)
    profile = root / f"deploy/vm/{phase}.env"
    current_policy = {
        "paper": "paper",
        "shadow": "book_guarded",
        "shadow-p14": "optimizer_shadow",
        "canary": "book_guarded",
    }[phase]
    profile.write_text(
        profile.read_text().replace(
            f"BFX_EXECUTION_POLICY={current_policy}",
            f"BFX_EXECUTION_POLICY={wrong_policy}",
        )
    )

    result = _run_deploy(root, phase, confirm=phase == "canary")

    assert result.returncode != 0
    assert "execution policy" in result.stdout
    assert not (root / "fake-docker.log").exists()


@pytest.mark.parametrize("phase", ["shadow", "shadow-p14", "canary"])
def test_deploy_script_rejects_live_capable_profile_without_book_evidence(
    tmp_path: Path, phase: str
) -> None:
    root = _deploy_root(tmp_path)
    profile = root / f"deploy/vm/{phase}.env"
    profile.write_text(
        "\n".join(
            line
            for line in profile.read_text().splitlines()
            if not line.startswith("BFX_BOOK_MAX_DOWN_PCT=")
        )
        + "\n"
    )

    result = _run_deploy(root, phase, confirm=phase == "canary")

    assert result.returncode != 0
    assert "BFX_BOOK_MAX_DOWN_PCT" in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_deploy_script_rejects_optimizer_live_without_model_evidence(tmp_path: Path) -> None:
    root = _deploy_root(tmp_path)
    profile = root / "deploy/vm/canary.env"
    profile.write_text(
        profile.read_text().replace(
            "BFX_EXECUTION_POLICY=book_guarded",
            "BFX_EXECUTION_POLICY=optimizer_live",
        )
    )

    result = _run_deploy(root, "canary", confirm=True)

    assert result.returncode != 0
    assert "BFX_FILL_MODEL_ARTIFACT" in result.stdout
    assert not (root / "fake-docker.log").exists()


@pytest.mark.parametrize(
    ("phase", "expected_policy"),
    [
        ("paper", "paper"),
        ("shadow", "book_guarded"),
        ("shadow-p14", "optimizer_shadow"),
    ],
)
def test_deploy_script_assembles_the_selected_non_canary_profile(
    tmp_path: Path, phase: str, expected_policy: str
) -> None:
    root = _deploy_root(tmp_path)

    result = _run_deploy(root, phase)

    assert result.returncode == 0, result.stderr
    runtime = parse_env_file(root / ".env.runtime")
    assert runtime["BFX_EXECUTION_POLICY"] == expected_policy
    assert runtime["BFX_PHASE"] == ("paper" if phase == "paper" else "shadow")
    if phase == "paper":
        assert not {key for key in runtime if key.startswith("BFX_BOOK_")}
    else:
        assert {
            "BFX_BOOK_MAX_AGE_SECONDS": "30",
            "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15",
            "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        }.items() <= runtime.items()
    if phase == "shadow-p14":
        assert runtime["BFX_CELLS_YAML"] == "/app/configs/cells.experimental-p14.yaml"
    assert "deployed phase=" + phase + " sha=safe-test-sha" in result.stdout
    assert len((root / "fake-docker.log").read_text().splitlines()) == 3


def test_shadow_p14_selected_cells_have_locked_adaptive_parameters() -> None:
    cells = load_cells_only(REPOSITORY_ROOT / "backend_py/configs/cells.experimental-p14.yaml")

    assert len(cells) == 4
    assert {
        (cell.params["p_mid"], cell.params["p_long"], cell.params["t1"], cell.params["t2"])
        for cell in cells
    } == {(7, 14, 0.5, 1.5)}


def test_canary_requires_confirmation_before_fake_docker_and_displays_safety_caps(
    tmp_path: Path,
) -> None:
    root = _deploy_root(tmp_path)

    result = _run_deploy(root, "canary")

    assert result.returncode != 0
    assert "canary deploy needs interactive confirmation" in result.stderr
    assert "per-symbol caps: caps: {fUSD: 400, fUST: 10000}" in result.stdout
    assert "BFX_ALLOCATION_CAP_USDT  : 0" in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_confirmed_canary_uses_canary_profile_and_reaches_only_fake_docker(
    tmp_path: Path,
) -> None:
    root = _deploy_root(tmp_path)

    result = _run_deploy(root, "canary", confirm=True)

    assert result.returncode == 0, result.stderr
    runtime = parse_env_file(root / ".env.runtime")
    assert runtime["BFX_EXECUTION_POLICY"] == "book_guarded"
    assert runtime["BFX_CELLS_YAML"] == "/app/configs/cells.canary.yaml"
    assert runtime["BFX_SAFETY_CONFIG"] == "/app/configs/safety.canary.yaml"
    assert (root / "fake-uv.log").read_text().split() == [
        "run",
        "python",
        "scripts/halt2_cutover.py",
        "preflight",
        "--account-id",
        "550e8400-e29b-41d4-a716-446655440000",
        "--environment",
        "prod",
        "--evidence",
        str(root.parent / "halt2-canary-evidence.json"),
        "--projector-version",
        "halt2-v1",
        "--image-digest",
        "safe-test-sha",
        "--config-artifact",
        str(root / "backend_py/configs/safety.canary.yaml"),
    ]
    assert len((root / "fake-docker.log").read_text().splitlines()) == 3


@pytest.mark.parametrize("field", ["BFX_PROJECTOR_VERSION", "BFX_HALT2_EVIDENCE_REPORT"])
def test_canary_rejects_missing_halt2_runtime_gate_before_docker(tmp_path: Path, field: str) -> None:
    root = _deploy_root(tmp_path)
    profile = root / "deploy/vm/canary.env"
    profile.write_text(
        "\n".join(line for line in profile.read_text().splitlines() if not line.startswith(field + "="))
        + "\n"
    )

    result = _run_deploy(root, "canary", confirm=True)

    assert result.returncode != 0
    assert field in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_canary_rejects_noncanonical_account_uuid_before_docker(tmp_path: Path) -> None:
    root = _deploy_root(tmp_path)
    bot_env = root.parent / "home/bfx/bot.env"
    bot_env.write_text(bot_env.read_text().replace("550e8400-e29b-41d4-a716-446655440000", "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"))

    result = _run_deploy(root, "canary", confirm=True)

    assert result.returncode != 0
    assert "BFX_EXCHANGE_ACCOUNT_ID must be a canonical UUID" in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_canary_preflight_failure_blocks_docker(tmp_path: Path) -> None:
    root = _deploy_root(tmp_path)
    fake_bin = root.parent / "fake-bin"
    _write_executable(
        fake_bin / "uv",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_UV_LOG\"\nexit 2\n",
    )

    result = _run_deploy(root, "canary", confirm=True)

    assert result.returncode != 0
    assert "Halt 2 preflight failed" in result.stdout
    assert (root / "fake-uv.log").exists()
    assert not (root / "fake-docker.log").exists()


@pytest.mark.parametrize("field", ["BFX_OPERATOR_USER_ID", "BFX_OPERATOR_ROLE"])
def test_deploy_script_rejects_missing_backend_operator_auth_before_docker(
    tmp_path: Path, field: str
) -> None:
    root = _deploy_root(tmp_path)
    webapi = root.parent / "home/bfx/webapi.env"
    lines = [line for line in webapi.read_text().splitlines() if not line.startswith(field + "=")]
    webapi.write_text("\n".join(lines) + "\n")

    result = _run_deploy(root, "paper")

    assert result.returncode != 0
    assert field in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_deploy_script_rejects_non_admin_backend_operator_role_before_docker(
    tmp_path: Path,
) -> None:
    root = _deploy_root(tmp_path)
    webapi = root.parent / "home/bfx/webapi.env"
    webapi.write_text(webapi.read_text().replace("BFX_OPERATOR_ROLE=admin", "BFX_OPERATOR_ROLE=user"))

    result = _run_deploy(root, "paper")

    assert result.returncode != 0
    assert "BFX_OPERATOR_ROLE" in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_deploy_script_rejects_frontend_backend_operator_id_mismatch_before_docker(
    tmp_path: Path,
) -> None:
    root = _deploy_root(tmp_path)
    frontend = root.parent / "home/bfx/frontend.env"
    frontend.write_text(frontend.read_text().replace("BFX_OPERATOR_USER_ID=operator-1", "BFX_OPERATOR_USER_ID=operator-2"))

    result = _run_deploy(root, "paper")

    assert result.returncode != 0
    assert "BFX_OPERATOR_USER_ID" in result.stdout
    assert "match" in result.stdout
    assert not (root / "fake-docker.log").exists()


@pytest.mark.parametrize("field", ["BFX_OPERATOR_USER_ID", "BFX_OPERATOR_ROLE"])
def test_deploy_script_rejects_missing_frontend_operator_auth_before_docker(
    tmp_path: Path, field: str
) -> None:
    root = _deploy_root(tmp_path)
    frontend = root.parent / "home/bfx/frontend.env"
    lines = [line for line in frontend.read_text().splitlines() if not line.startswith(field + "=")]
    frontend.write_text("\n".join(lines) + "\n")

    result = _run_deploy(root, "paper")

    assert result.returncode != 0
    assert field in result.stdout
    assert not (root / "fake-docker.log").exists()


def test_deploy_script_rejects_non_admin_frontend_operator_role_before_docker(
    tmp_path: Path,
) -> None:
    root = _deploy_root(tmp_path)
    frontend = root.parent / "home/bfx/frontend.env"
    frontend.write_text(frontend.read_text().replace("BFX_OPERATOR_ROLE=admin", "BFX_OPERATOR_ROLE=user"))

    result = _run_deploy(root, "paper")

    assert result.returncode != 0
    assert "BFX_OPERATOR_ROLE" in result.stdout
    assert not (root / "fake-docker.log").exists()
