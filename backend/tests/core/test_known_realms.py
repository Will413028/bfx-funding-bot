"""Every list of realms agrees with ``KNOWN_REALMS``; the enum is the one copy that cannot
derive from it (contract ``core-is-leaf``). Mutation: add a member to either side."""
from __future__ import annotations

from sqlalchemy import CheckConstraint

from bfx_funding_bot.core.database_realm import DatabaseRealmRow
from bfx_funding_bot.core.realms import KNOWN_REALMS
from bfx_funding_bot.core.settings import VALID_DEPLOYMENT_ENVIRONMENTS
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment


def test_the_realm_lists_agree() -> None:
    assert tuple(env.value for env in DeploymentEnvironment) == KNOWN_REALMS
    assert frozenset(KNOWN_REALMS) == VALID_DEPLOYMENT_ENVIRONMENTS
    checks = [c for c in DatabaseRealmRow.__table__.constraints
              if isinstance(c, CheckConstraint) and c.name == "ck_database_realm_realm"]
    assert [str(c.sqltext) for c in checks] == ["realm IN ('prod', 'ci')"]


def test_the_ci_member_is_the_simulated_realm() -> None:
    # Mutation: rename DeploymentEnvironment.CI's value; the simulated venue's realm drifts.
    from bfx_funding_bot.modules.simulated_venue.contracts import SIMULATED_REALM

    assert DeploymentEnvironment.CI.value == SIMULATED_REALM
