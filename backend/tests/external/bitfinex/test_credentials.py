"""Regression coverage for credential values in object representations."""

from decimal import Decimal

from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.modules.execution.protocols import AccountContext


def test_credentials_and_account_context_reprs_hide_api_values() -> None:
    api_key = "fake-api-key-for-repr-test"
    api_secret = "fake-api-secret-for-repr-test"
    credentials = Credentials(api_key=api_key, api_secret=api_secret)
    context = AccountContext(
        account_id="fake-account-id",
        credentials=credentials,
        allocation_cap_usdt=Decimal("1000"),
    )

    for obj in (credentials, context):
        for rendered in (repr(obj), str(obj)):
            assert api_key not in rendered
            assert api_secret not in rendered
