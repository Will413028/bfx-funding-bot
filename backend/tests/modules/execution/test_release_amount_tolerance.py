"""Consumption must accept exactly the revision the reconciler may make.

The venue minimum is USD-denominated, so its UST equivalent drifts with FX
between authorisation and submission. The reconciler revises up to
RELEASE_MINIMUM_TOLERANCE and logs `release_amount_revised`; consumption used to
demand exact equality, so those revised submissions died as
`session_decision_mismatch` -- after the offer had been priced and the DR window
spent. Any FX movement at all was enough, which in practice meant every session.
"""
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.deployment.reconciler import RELEASE_MINIMUM_TOLERANCE
from bfx_funding_bot.modules.execution.release_session import _within_authorised_amount
from bfx_funding_bot.modules.execution.release_tables import ReleaseSessionRow


def _row(minimum: str | None, maximum: str = "10000") -> ReleaseSessionRow:
    return ReleaseSessionRow(
        minimum_amount=None if minimum is None else Decimal(minimum),
        max_amount=Decimal(maximum),
    )


def test_exact_authorised_amount_is_accepted() -> None:
    assert _within_authorised_amount(Decimal("150.01425136"), _row("150.01425136"))


def test_the_revision_that_actually_happened_is_accepted() -> None:
    """The real numbers from the 2026-09-22 session that this rejected."""
    assert _within_authorised_amount(Decimal("150.01575166"), _row("150.01425136"))


def test_revision_up_to_the_tolerance_is_accepted() -> None:
    minimum = Decimal("150")
    assert _within_authorised_amount(minimum * (Decimal(1) + RELEASE_MINIMUM_TOLERANCE), _row("150"))


def test_revision_past_the_tolerance_is_rejected() -> None:
    minimum = Decimal("150")
    over = minimum * (Decimal(1) + RELEASE_MINIMUM_TOLERANCE) + Decimal("0.01")
    assert not _within_authorised_amount(over, _row("150"))


def test_the_operator_cap_still_binds_inside_the_tolerance() -> None:
    """max_amount wins over the tolerance; the human's ceiling is never revised past."""
    assert not _within_authorised_amount(Decimal("151"), _row("150", maximum="150.5"))


@pytest.mark.parametrize("submitted", ["149.99", "0", "-1"])
def test_amounts_below_the_authorised_minimum_are_rejected(submitted: str) -> None:
    """Revision only ever moves up; anything under is a different action."""
    assert not _within_authorised_amount(Decimal(submitted), _row("150"))


def test_missing_minimum_is_rejected() -> None:
    assert not _within_authorised_amount(Decimal("150"), _row(None))
