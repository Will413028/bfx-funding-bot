import bfx_funding_bot.modules.execution.diagnostics.tables  # noqa: F401  (register)
from bfx_funding_bot.core.db import Base


def test_diagnostics_table_registered() -> None:
    assert "diagnostics" in Base.metadata.tables


def test_diagnostics_columns() -> None:
    cols = Base.metadata.tables["diagnostics"].columns
    for name in ("id", "account_id", "deployment_environment", "kind",
                 "payload", "occurred_at", "recorded_at"):
        assert name in cols, name
    assert cols["id"].primary_key is True


def test_diagnostics_index_present() -> None:
    idx_names = {i.name for i in Base.metadata.tables["diagnostics"].indexes}
    assert "idx_diagnostics_acct_occurred" in idx_names
