"""Tests for the explicit operator-side canary permit command."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from scripts.issue_canary_permit import _parser, _scope_from_args


def test_issue_permit_parser_produces_an_account_local_fixed_scope() -> None:
    args = _parser().parse_args([
        "--account-id", "11111111-1111-1111-1111-111111111111",
        "--environment", "prod",
        "--symbol", "fUST",
        "--cell", "fUST_a30",
        "--strategy", "mean_reversion",
        "--amount-usdt", "150",
        "--operator-id", "operator-1",
    ])

    scope = _scope_from_args(args)

    assert scope.account_id == UUID("11111111-1111-1111-1111-111111111111")
    assert scope.environment == "prod"
    assert scope.symbol == "fUST"
    assert scope.cell == "fUST_a30"
    assert scope.strategy == "mean_reversion"
    assert scope.amount_usdt == Decimal("150")


def test_issue_permit_parser_rejects_non_positive_amount() -> None:
    args = _parser().parse_args([
        "--account-id", "11111111-1111-1111-1111-111111111111",
        "--environment", "prod",
        "--symbol", "fUST",
        "--cell", "fUST_a30",
        "--strategy", "mean_reversion",
        "--amount-usdt", "0",
        "--operator-id", "operator-1",
    ])

    with pytest.raises(ValueError, match="positive"):
        _scope_from_args(args)
