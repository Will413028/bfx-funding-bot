"""Tests for AxiomConfig.from_env — env-binding with fail-loud validation."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from bfx_funding_bot.external.axiom import AxiomConfig
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment


class TestFromEnv:
    @pytest.fixture
    def base_env(self) -> dict[str, str]:
        return {
            "AXIOM_API_KEY": "axk_test",
            "AXIOM_DATASET": "bfx-funding-bot-ci",
            "BFX_DEPLOYMENT_ENV": "ci",
        }

    def test_happy_path(self, base_env: dict[str, str]) -> None:
        with patch.dict(os.environ, base_env, clear=True):
            cfg = AxiomConfig.from_env()
        assert cfg.api_key == "axk_test"
        assert cfg.dataset == "bfx-funding-bot-ci"
        assert cfg.deployment_env is DeploymentEnvironment.CI

    @pytest.mark.parametrize("env_value", ["prod", "shadow", "ci"])
    def test_all_three_envs_accepted(
        self, base_env: dict[str, str], env_value: str
    ) -> None:
        env = {**base_env, "BFX_DEPLOYMENT_ENV": env_value}
        with patch.dict(os.environ, env, clear=True):
            cfg = AxiomConfig.from_env()
        assert cfg.deployment_env.value == env_value

    def test_missing_deployment_env_raises(self, base_env: dict[str, str]) -> None:
        env = {k: v for k, v in base_env.items() if k != "BFX_DEPLOYMENT_ENV"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="BFX_DEPLOYMENT_ENV"),
        ):
            AxiomConfig.from_env()

    def test_missing_api_key_raises(self, base_env: dict[str, str]) -> None:
        env = {k: v for k, v in base_env.items() if k != "AXIOM_API_KEY"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(KeyError, match="AXIOM_API_KEY"),
        ):
            AxiomConfig.from_env()

    def test_invalid_deployment_env_raises(self, base_env: dict[str, str]) -> None:
        env = {**base_env, "BFX_DEPLOYMENT_ENV": "staging"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError),
        ):
            AxiomConfig.from_env()
