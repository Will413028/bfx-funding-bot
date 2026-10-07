"""The whole runtime schema on ``Base.metadata``: importing this module registers every table.

Table modules reference each other's tables by name, and the module boundaries forbid some of
those imports (ledger cannot import accounts), so a module alone may leave a foreign key
unresolved. Alembic autogenerate and the test fixtures import this module instead of a
hand-picked subset; tests/apps/test_schema.py fails when a table module is missing here.
"""
# ruff: noqa: F401

import bfx_funding_bot.core.database_realm
import bfx_funding_bot.modules.accounts.exchange_accounts
import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.accounts.user_profile
import bfx_funding_bot.modules.candles.tables
import bfx_funding_bot.modules.deployments.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.capital_tables
import bfx_funding_bot.modules.execution.diagnostics.tables
import bfx_funding_bot.modules.execution.safety.tables
import bfx_funding_bot.modules.execution.uncertainty_tables
import bfx_funding_bot.modules.external_signals.tables
import bfx_funding_bot.modules.funding_stats.tables
import bfx_funding_bot.modules.ledger.tables
import bfx_funding_bot.modules.lending.tracking.tables
import bfx_funding_bot.modules.live_validation.tables
import bfx_funding_bot.modules.marketfeed.tables
import bfx_funding_bot.modules.observability.tables
import bfx_funding_bot.modules.simulated_venue.tables
